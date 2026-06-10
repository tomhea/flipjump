"""
breakpoint handling for the interpreter's debugger.
resolves breakpoints (by address, exact label, or label-substring), maps addresses
back to labels, and pauses the run to let the user inspect memory and program state.
"""

import re
from pathlib import Path
from typing import Optional, Dict, Set, Tuple

from flipjump.fjm import fjm_reader
from flipjump.interpretter.debugging.message_boxes import (
    display_message_box,
    display_message_box_and_get_text_answer,
    display_message_box_with_choices_and_get_answer,
)
from flipjump.utils.classes import RunStatistics
from flipjump.utils.constants import MACRO_SEPARATOR_STRING
from flipjump.utils.exceptions import FlipJumpException
from flipjump.utils.functions import load_debugging_labels


class BreakpointHandlerUnnecessary(Exception):
    pass


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
        display_message_box(
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
            display_message_box(
                body_message=f'Reading {user_query}:\n'
                f'memory[{hex(address)}] = {memory_word_value}  (or {hex(memory_word_value)}).'
                f'{label_name}',
                title_message='Read Memory',
            )
            return

        value, first_address, last_address = calculate_variable_value(variable_prefix, address, mem)
        display_message_box(
            body_message=f'Reading the variable {user_query}:\n'
            f'memory[{hex(first_address)}, {hex(last_address)})'
            f' = {value}  (or {hex(value)}).'
            f'{label_name}',
            title_message='Reading FlipJump Variable',
        )

    except FlipJumpException as fje:
        display_message_box(
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

    def get_message_box_body(self, ip: int, mem: fjm_reader.Reader, op_counter: int) -> str:
        """
        @return the message box body for the debug-action query, for the current ip.
        """
        address = self.get_address_str(ip)
        flip = self.get_address_str(mem.get_word(ip))
        jump = self.get_address_str(mem.get_word(ip + mem.memory_width))
        return f'Address {address}.\n\n{op_counter} ops executed.\n\nflip {flip}.\n\njump {jump}.'

    def handle_read_memory(self, mem: fjm_reader.Reader) -> None:
        """
        This function queries the user for reading a memory-word (by its address or a label).
        It then shows the value of the requested word, and more useful information about it.
        The function also support reading flipjump variables (saved in label+dbit+i*dw) with the ':' prefix
         (for example ':b32:integer_label').

        @param mem: the fjm_reader.Reader for the current running fj. Used for reading the actual memory values
         of the given address (or addresses if the user asked for a variable).
        """
        result = display_message_box_and_get_text_answer(
            body_message="What memory-word would you like to read?\n"
            "Use any of these options:\n"
            "- Decimal number\n"
            "- Hexadecimal number with the '0x' prefix\n"
            "- A full label name\n\n"
            'You can read the jump-words of a label by using the  ":j:"  prefix.\n\n'
            "You can also read flipjump variables (like bit.vec, hex.vec, ..),\n"
            " with the following prefixes to the address:\n"
            '- :bN: read bit variable (like ":b32:integer_label")\n'
            '- :hN: read hex variable (like ":h8:integer_label")\n'
            '- :BN: read byte variable (holds 8 bits after the dbit)'
            ' (like ":B4:integer_label")\n\n'
            "Also, you can index a variable in an array, by using the ':*:N:* prefix':\n"
            '- (like ":B4:7:integer_array_label" - this will access the 7th integer\n'
            '   in the array, so starting from byte 28 of the array\'s variables)\n'
            '- (you can also use the ":f/j:N:label" to skip N ops forward,\n'
            '   and also ":f/j:n:label:N" to skip n*N ops forward)\n',
            title_message='Debug: read memory address',
        )
        if result is None:
            return

        variable_prefix = None
        query = result
        match = re.match(r':([bhBfj])(\d*):(\d+:)?([^:]*)', result)
        if match:
            variable_type, variable_length, index_string, result = match.groups()
            if variable_length == '':
                variable_length = '1'
            index = int(index_string[:-1]) if index_string else 0
            variable_prefix = (variable_type, int(variable_length), index)

        if result in self.label_to_address:
            show_memory_address(variable_prefix, query, self.label_to_address[result], mem, None)
            return

        try:
            address = int(result)
            show_memory_address(variable_prefix, query, address, mem, self.get_address_str(address))
        except ValueError:
            try:
                address = int(result, 16)
                show_memory_address(variable_prefix, query, address, mem, self.get_address_str(address))
            except ValueError:
                display_message_box(
                    body_message=f"Failed, can't resolve the address/label \"{query}\".\n"
                    f"You entered an invalid memory-address, "
                    f"or the label you entered wasn't in its full form "
                    f"(remember the '---' parts. for more info read the flipjump/README.md in github).",
                    title_message='Invalid memory address.',
                )

    def query_user_for_debug_action(self, ip: int, mem: fjm_reader.Reader, op_counter: int) -> str:
        """
        query the user for the next debug-action to make, while debugging (single-step, continue, ...)
        @return: The chosen debug-action string.
        """
        title = "Breakpoint" if ip in self.breakpoints else "Debug Step"
        body = self.get_message_box_body(ip, mem, op_counter)
        actions = ['Read Memory', 'Single Step', 'Skip 10', 'Skip 100', 'Continue', 'Continue All']

        action = display_message_box_with_choices_and_get_answer(body, title, actions, 'Continue All')
        while action == 'Read Memory':
            self.handle_read_memory(mem)
            action = display_message_box_with_choices_and_get_answer(body, title, actions, 'Continue All')

        return action

    def apply_debug_action(self, action: str, op_counter: int) -> None:
        """
        @raise BreakpointHandlerUnnecessary for the "Continue All" action
        """
        if action == 'Single Step':
            self.next_break = op_counter + 1
        elif action == 'Skip 10':
            self.next_break = op_counter + 10
        elif action == 'Skip 100':
            self.next_break = op_counter + 100
        elif action == 'Continue':
            self.next_break = None
        elif action == 'Continue All':
            self.next_break = None
            raise BreakpointHandlerUnnecessary()


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
    print(f': {action}')

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
    if breakpoint_contains_labels:
        for label in tuple(label_to_address)[::-1]:
            for bcl in breakpoint_contains_labels:
                if bcl in label:
                    address = label_to_address[label]
                    breakpoints[address] = label


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
