"""
breakpoint handling for the interpreter's debugger.
resolves breakpoints (by address, exact label, or label-substring), maps addresses
back to labels, and pauses the run to let the user inspect memory and program state.
"""

import re
from bisect import bisect_right
from itertools import accumulate
from pathlib import Path
from typing import Optional, Dict, Set, Tuple

from flipjump.fjm import fjm_reader
from flipjump.interpreter.debugging.user_queries import ask_for_command, show_message
from flipjump.utils.classes import RunStatistics
from flipjump.utils.constants import MACRO_SEPARATOR_STRING
from flipjump.utils.exceptions import FlipJumpException
from flipjump.utils.functions import load_debugging_labels


class BreakpointHandlerUnnecessary(Exception):
    pass


DEBUGGER_HELP = (
    "commands (one per line):\n"
    "  h / help / ?            show this help\n"
    "  r / read  TARGET        read a memory-word / flipjump-variable (formats below)\n"
    "  s / step                execute one op\n"
    "  s / skip  N             execute N more ops, then stop (N decimal or 0x-hex)\n"
    "  c / cont / continue     run to the next breakpoint\n"
    "  c* / ca / continue all  run to the end, ignoring all breakpoints\n"
    "  q / quit / exit         stop the run (a keyboard-interrupt; Ctrl+C / EOF also work)\n"
    "\n"
    "the read TARGET can be:\n"
    "  - a decimal, or a 0x-hex, memory address\n"
    "  - a full label name (its address)\n"
    "  - a flipjump variable, with a ':type:' prefix on a label/address:\n"
    "      :bN:  a bit  vector  (e.g. :b32:integer_label)\n"
    "      :hN:  a hex  vector  (e.g. :h8:integer_label  - an 8-hex / 32-bit value)\n"
    "      :BN:  a byte vector  (e.g. :B4:integer_label  - 4 packed bytes)\n"
    "  - index into an array of those: :B4:7:array_label reads the 7th 4-byte cell\n"
    "  - :f:N:label / :j:N:label  read the flip / jump word N ops past the label\n"
    "      (and :f/j:n:label:N  to step n*N ops forward)"
)


def calculate_variable_value(
    variable_prefix: Tuple[str, int, int], address: int, mem: fjm_reader.Reader
) -> Tuple[int, int, int]:
    """
    Read the variable related memory words (using 'mem'),
     and return the value of the word created by this bit/hex/Byte vector.
    The memory words are pointed by the given 'address' and 'variable_prefix'.
    """
    w = mem.memory_width
    variable_type, variable_length, index = variable_prefix
    index_offset_in_w = 2 * variable_length * index

    first_address = address + index_offset_in_w * w
    last_address = first_address + 2 * w * variable_length
    variable_memory_words = [
        mem.get_word(current_address) for current_address in range(first_address + w, last_address, 2 * w)
    ]
    bits_per_word = {'b': 1, 'h': 4, 'B': 8}[variable_type]

    value = 0
    for word in variable_memory_words[::-1]:
        data_bits = (word >> w.bit_length()) & ((1 << bits_per_word) - 1)
        value = (value << bits_per_word) | data_bits

    return value, first_address, last_address


def handle_read_f_j(
    variable_prefix: Optional[Tuple[str, int, int]], address: int, label_name: Optional[str], w: int
) -> Tuple[Optional[Tuple[str, int, int]], int, str]:
    """
    If variable_type is f/j, modify the address accordingly, them set variable_prefix = None.
    Anyway, also create a label_name string and return the new one.
    """
    if variable_prefix and variable_prefix[0] in ('f', 'j'):
        variable_type, variable_length, index = variable_prefix

        added_w = 2 * variable_length * index
        if variable_type == 'j':
            added_w += 1
        if label_name is not None and added_w != 0:
            label_name += f' + {added_w}w'

        address += w * added_w
        variable_prefix = None

    label_name = '' if label_name is None else f'\n\nThis address also goes by this label name:\n\n{label_name}'

    return variable_prefix, address, label_name


def show_memory_address(
    variable_prefix: Optional[Tuple[str, int, int]],
    user_query: str,
    address: int,
    mem: fjm_reader.Reader,
    label_name: Optional[str],
) -> None:
    """
    Shows the value of the requested memory address / variable.
    The function also support reading flipjump variables (saved in label+dbit+i*dw).
    Also shows the label-name of the address if the user entered an integer-address.

    @param variable_prefix: if not None: the user asked for a flipjump variable: tuple
     (
      variable_type - 'b'/'h'/'B' for bit/hex/byte,
      variable_length - number of memory-ops,
      index - the index of the variable, in an array starting from address, and of cells variable_type[:variable_length]
     ).
    @param user_query: the string the user entered.
    @param address: the address resolved from user_query string.
    @param mem: the fjm_reader.Reader for the current running fj. Used for reading the actual memory values
     of the given address (or addresses if the user asked for a variable).
    @param label_name: if not None - the user asked for an integer address, and this its label-name,
     or the closest label to it.
    """
    w = mem.memory_width

    if address % w != 0 or address < 0 or address >= (1 << w):
        show_message(
            body_message=f"Failed while trying to read {user_query}:\n"
            f" The requested memory address ({address}) must be aligned"
            f" (must be divisible by {w}),\n"
            f" Can't be negative, and must be smaller than {hex(1 << w)}.",
            title_message='Bad memory address',
        )
        return

    try:
        variable_prefix, address, label_name = handle_read_f_j(variable_prefix, address, label_name, w)

        if variable_prefix is None:
            memory_word_value = mem.get_word(address)
            show_message(
                body_message=f'Reading {user_query}:\n'
                f'memory[{hex(address)}] = {memory_word_value}  (or {hex(memory_word_value)}).'
                f'{label_name}',
                title_message='Read Memory',
            )
            return

        value, first_address, last_address = calculate_variable_value(variable_prefix, address, mem)
        show_message(
            body_message=f'Reading the variable {user_query}:\n'
            f'memory[{hex(first_address)}, {hex(last_address)})'
            f' = {value}  (or {hex(value)}).'
            f'{label_name}',
            title_message='Reading FlipJump Variable',
        )

    except FlipJumpException as fje:
        show_message(
            body_message=f"Failed while trying to read {user_query}:\n"
            f"Failed to read address {address}, with the error: {fje}.\n"
            f"Maybe this memory region isn't initialized in the currently running .fjm?",
            title_message='Read Memory Failure',
        )


def get_nice_label_repr(label: str, pad: int = 0) -> str:
    """
    @return: a well-formed string that represents the label (padded with 'pad' spaces).
    """
    parts = label.split(MACRO_SEPARATOR_STRING)
    return ' ->\n'.join(f"{' ' * (pad + i)}{part}" for i, part in enumerate(parts))


class BreakpointHandler:
    """
    Handle breakpoints (know when breakpoints happen, query user for action).
    """

    def __init__(
        self, breakpoints: Dict[int, Optional[str]], address_to_label: Dict[int, str], label_to_address: Dict[str, int]
    ):
        self.breakpoints = breakpoints
        self.address_to_label = address_to_label
        self.label_to_address = label_to_address

        if self.address_to_label and 0 not in self.address_to_label:
            self.address_to_label[0] = ':memory-start:'

        self.next_break: Optional[int] = None  # will break(point) when the number of executed ops reaches this number.

    def should_break(self, ip: int, op_counter: int) -> bool:
        return self.next_break == op_counter or ip in self.breakpoints

    def get_address_str(self, address: int) -> str:
        """
        @return: a string that the must debugging-useful information we know about a memory address, in a pretty way.
        If this address has a label then return it.
        Else, return the closest previous-address label to it, and state also the offset.
        If cant be found, just return the address. All labels returned are more pretty-formatted.
        """
        if address in self.breakpoints and self.breakpoints[address] is not None:
            label_repr = get_nice_label_repr(self.breakpoints[address], pad=4)  # type: ignore[arg-type]
            return f'{hex(address)}:\n{label_repr}'
        elif address in self.address_to_label:
            label_repr = get_nice_label_repr(self.address_to_label[address], pad=4)
            return f'{hex(address)}:\n{label_repr}'
        else:
            try:
                address_before = max(a for a in self.address_to_label if a <= address)
                label_repr = get_nice_label_repr(self.address_to_label[address_before], pad=4)
                return f'{hex(address)} ({hex(address - address_before)} bits after:)\n{label_repr}'
            except ValueError:
                return f'{hex(address)}'

    def get_breakpoint_message_body(self, ip: int, mem: fjm_reader.Reader, op_counter: int) -> str:
        """
        @return the message box body for the debug-action query, for the current ip.
        """
        address = self.get_address_str(ip)
        flip = self.get_address_str(mem.get_word(ip))
        jump = self.get_address_str(mem.get_word(ip + mem.memory_width))
        return f'Address {address}.\n\n{op_counter} ops executed.\n\nflip {flip}.\n\njump {jump}.'

    def handle_read_memory(self, target: str, mem: fjm_reader.Reader) -> None:
        """
        Reads the memory-word / flipjump-variable named by 'target' (an address, a label, or a
        ':'-prefixed flipjump-variable - see DEBUGGER_HELP), and shows its value and the most
        useful information we know about it.

        @param target: the read target the user typed after the read command.
        @param mem: the fjm_reader.Reader for the current running fj. Used for reading the actual memory values
         of the given address (or addresses if the user asked for a variable).
        """
        variable_prefix = None
        query = target
        match = re.match(r':([bhBfj])(\d*):(\d+:)?([^:]*)', target)
        if match:
            variable_type, variable_length, index_string, target = match.groups()
            if variable_length == '':
                variable_length = '1'
            index = int(index_string[:-1]) if index_string else 0
            variable_prefix = (variable_type, int(variable_length), index)

        if target in self.label_to_address:
            show_memory_address(variable_prefix, query, self.label_to_address[target], mem, None)
            return

        try:
            address = int(target)
            show_memory_address(variable_prefix, query, address, mem, self.get_address_str(address))
        except ValueError:
            try:
                address = int(target, 16)
                show_memory_address(variable_prefix, query, address, mem, self.get_address_str(address))
            except ValueError:
                show_message(
                    body_message=f"Failed, can't resolve the address/label \"{query}\".\n"
                    f"You entered an invalid memory-address, "
                    f"or the label you entered wasn't in its full form "
                    f"(remember the '---' parts. for more info read the flipjump/README.md in github).",
                    title_message='Invalid memory address.',
                )

    def query_user_for_debug_action(self, ip: int, mem: fjm_reader.Reader, op_counter: int) -> Tuple[str, int]:
        """
        Run the debugger prompt at the current breakpoint, handling read/help inline and looping
        until the user picks an action that resumes the run.
        @return: (command, argument) - command is one of 'step' / 'skip' / 'continue' /
         'continue_all' / 'exit'; argument is the skip-count for 'skip' (else 0).
        """
        title = "Breakpoint" if ip in self.breakpoints else "Debug Step"
        show_message(self.get_breakpoint_message_body(ip, mem, op_counter), title)

        while True:
            line = ask_for_command('(debug, "h" for help) > ')
            if line is None:  # EOF / Ctrl+D
                return ('exit', 0)
            if not line:  # empty line - just re-prompt
                continue

            tokens = line.split()
            command, argument = tokens[0].lower(), (tokens[1] if len(tokens) > 1 else None)

            if command in ('h', 'help', '?'):
                show_message(DEBUGGER_HELP, 'Debugger commands')
            elif command in ('r', 'read'):
                if argument is None:
                    show_message("usage: read <address/label/:variable:>  ('help' lists the formats).", 'Debugger')
                else:
                    self.handle_read_memory(' '.join(tokens[1:]), mem)
            elif command in ('s', 'step') and argument is None:
                return ('step', 0)
            elif command in ('s', 'skip') and argument is not None:
                try:
                    count = int(argument, 0)  # accepts decimal and 0x-hex
                except ValueError:
                    show_message(f"skip needs a number (decimal or 0x-hex), got {argument!r}.", 'Debugger')
                    continue
                if count <= 0:
                    show_message(f"skip needs a positive count, got {count}.", 'Debugger')
                    continue
                return ('skip', count)
            elif command in ('c', 'cont', 'continue') and argument is None:
                return ('continue', 0)
            elif command in ('c*', 'ca') or line.lower() == 'continue all':
                return ('continue_all', 0)
            elif command in ('q', 'quit', 'exit'):
                return ('exit', 0)
            else:
                show_message(f"unknown command {line!r}. type 'h' (or 'help') for the commands.", 'Debugger')

    def apply_debug_action(self, action: Tuple[str, int], op_counter: int) -> None:
        """
        @param action: the (command, argument) returned by query_user_for_debug_action.
        @raise BreakpointHandlerUnnecessary for 'continue_all' (the handler is no longer needed).
        @raise KeyboardInterrupt for 'exit' (stops the run as a keyboard-interrupt).
        """
        command, argument = action
        if command == 'step':
            self.next_break = op_counter + 1
        elif command == 'skip':
            self.next_break = op_counter + argument
        elif command == 'continue':
            self.next_break = None
        elif command == 'continue_all':
            self.next_break = None
            raise BreakpointHandlerUnnecessary()
        elif command == 'exit':
            raise KeyboardInterrupt()


def handle_breakpoint(
    breakpoint_handler: BreakpointHandler, ip: int, mem: fjm_reader.Reader, statistics: RunStatistics
) -> Optional[BreakpointHandler]:
    """
    show debug message, query user for action, apply its action.
    @param breakpoint_handler: the breakpoint handler
    @param ip: current ip
    @param mem: the memory
    @param statistics: the statistics of the current run
    @return: the breakpoint handler (or None if it is not necessary anymore)
    """
    print('  program break', end="", flush=True)
    with statistics.pause_timer:
        action = breakpoint_handler.query_user_for_debug_action(ip, mem, statistics.op_counter)
    print(f': {action[0]}')

    try:
        breakpoint_handler.apply_debug_action(action, statistics.op_counter)
        return breakpoint_handler
    except BreakpointHandlerUnnecessary:
        return None


def get_breakpoints(
    breakpoint_addresses: Optional[Set[int]],
    breakpoint_labels: Optional[Set[str]],
    breakpoint_contains_labels: Optional[Set[str]],
    label_to_address: Dict[str, int],
) -> Dict[int, Optional[str]]:
    """
    generate the breakpoints' dictionary
    """
    breakpoints: Dict[int, Optional[str]] = {}

    update_breakpoints_from_addresses_set(breakpoint_addresses, breakpoints)
    update_breakpoints_from_breakpoint_contains_set(breakpoint_contains_labels, breakpoints, label_to_address)
    update_breakpoints_from_breakpoint_set(breakpoint_labels, breakpoints, label_to_address)

    return breakpoints


def update_breakpoints_from_breakpoint_set(
    breakpoint_labels: Optional[Set[str]], breakpoints: Dict[int, Optional[str]], label_to_address: Dict[str, int]
) -> None:
    """
    add breakpoints from breakpoint_labels.
    param breakpoints[in,out] - adds breakpoints to it
    """
    if breakpoint_labels:
        for bl in breakpoint_labels:
            if bl not in label_to_address:
                print(f"Warning:  Breakpoint label {bl} can't be found!")
            else:
                address = label_to_address[bl]
                breakpoints[address] = bl


def update_breakpoints_from_breakpoint_contains_set(
    breakpoint_contains_labels: Optional[Set[str]],
    breakpoints: Dict[int, Optional[str]],
    label_to_address: Dict[str, int],
) -> None:
    """
    add breakpoints generated with breakpoint_contains_labels.
    param breakpoints[in,out] - adds breakpoints to it
    """
    labels = tuple(label_to_address)
    if breakpoint_contains_labels and labels:
        # one search of all the labels' text per string, instead of a Python loop over every label per string
        text = '\n'.join(labels)  # no label holds a newline
        starts = list(accumulate((len(label) + 1 for label in labels), initial=0))
        found: Set[int] = set()
        for bcl in breakpoint_contains_labels:
            index = -1 if '\n' in bcl else text.find(bcl)
            while index != -1:
                label_index = bisect_right(starts, index) - 1
                found.add(label_index)
                index = text.find(bcl, starts[label_index + 1])  # a label is found once
        for label_index in sorted(found, reverse=True):  # an address keeps its first label
            label = labels[label_index]
            breakpoints[label_to_address[label]] = label


def update_breakpoints_from_addresses_set(
    breakpoint_addresses: Optional[Set[int]], breakpoints: Dict[int, Optional[str]]
) -> None:
    """
    add breakpoints of addresses breakpoint_addresses.
    param breakpoints[in,out] - adds breakpoints to it
    """
    if breakpoint_addresses:
        for address in breakpoint_addresses:
            breakpoints[address] = None


def load_labels_dictionary(debugging_file: Optional[Path], labels_file_needed: bool) -> Dict[str, int]:
    """
    load the labels_dictionary from debugging_file, if possible.
    @param debugging_file: The debugging file. If None: return an empty dictionary.
    @param labels_file_needed: If True, prints a warning if debugging-file is None
    @return: the label-to-address dictionary
    """
    if debugging_file is None:
        if labels_file_needed:
            print("Warning:  debugging labels can't be found! no debugging file specified.")
        return {}

    if not debugging_file.is_file():
        print(f"Warning:  debugging file {debugging_file} can't be found!")
        return {}

    return load_debugging_labels(debugging_file)


def get_breakpoint_handler(
    debugging_file: Optional[Path],
    breakpoint_addresses: Optional[Set[int]],
    breakpoint_labels: Optional[Set[str]],
    breakpoint_contains_labels: Optional[Set[str]],
) -> BreakpointHandler:
    """
    generate the breakpoint handler from the debugging file and the breakpoint sets.
    @param debugging_file: the debug file path (created at assemble time)
    @param breakpoint_addresses: set of addresses to break at
    @param breakpoint_labels: set of labels to break at
    @param breakpoint_contains_labels: set of strings, to break at every label that contains one of them
    @return: the breakpoint handler
    """
    labels_file_needed = any((breakpoint_addresses, breakpoint_contains_labels))
    label_to_address = load_labels_dictionary(debugging_file, labels_file_needed)

    address_to_label: Dict[int, str] = {}
    for label, address in label_to_address.items():
        if address in address_to_label:
            if len(label) >= len(address_to_label[address]):
                continue
        address_to_label[address] = label

    breakpoints = get_breakpoints(breakpoint_addresses, breakpoint_labels, breakpoint_contains_labels, label_to_address)

    return BreakpointHandler(breakpoints, address_to_label, label_to_address)
