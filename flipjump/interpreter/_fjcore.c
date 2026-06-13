/*
 * _fjcore - the native FlipJump interpreter engine.
 *
 * Implements a Memory object (segment-aware, lazily-allocated paged memory) and a run()
 * method executing the FlipJump fetch-flipjump loop in C. The semantics exactly mirror
 * the pure-Python fast loop in fjm_run.py (which mirrors the featured loop):
 *   - per-op order: read flip-word, output-check, input-check, flip, read jump-word,
 *     looping/null-ip termination checks, jump.
 *   - reads of in-segment untouched words are 0; reads outside any segment either
 *     terminate the run (garbage_stop) or read 0 (continue).
 *   - IO is routed through the Python io_device's read_bit/write_bit callbacks.
 *
 * Only the run-loop lives in C; the .fjm parsing, devices and debugger stay in Python.
 */

#define PY_SSIZE_T_CLEAN
/* the stable ABI (abi3): one wheel per platform/arch covers every CPython >= 3.10 */
#ifndef Py_LIMITED_API
#define Py_LIMITED_API 0x030A0000
#endif
#include <Python.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

/* force-inline so run_flat_loop's literal width/ww arguments constant-fold per width */
#if defined(_MSC_VER)
#  define FJ_ALWAYS_INLINE __forceinline
#else
#  define FJ_ALWAYS_INLINE inline __attribute__((always_inline))
#endif

#ifdef _WIN32
#include <windows.h>
#endif

/* a monotonic wall clock in seconds - matches the python reference pause-timer, which uses
   wall time around blocking IO reads (C clock() is process-CPU time on POSIX: ~0 while
   blocked on input, which would under-report the paused time there). */
static double monotonic_seconds(void)
{
#ifdef _WIN32
    LARGE_INTEGER frequency, counter;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&counter);
    return (double)counter.QuadPart / (double)frequency.QuadPart;
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
#endif
}

#define PAGE_BITS 14
#define PAGE_WORDS (1ull << PAGE_BITS) /* 16K words, 128KB per page */
#define PAGE_MASK (PAGE_WORDS - 1)

/* how often to check for signals (Ctrl+C): every 2^18 ops */
#define SIGNAL_CHECK_MASK 0x3FFFFull

/* flat-storage mode: programs whose segments all end below a word-count limit (and with
   w<=32, so bit 63 is free to mark out-of-segment words) use one dense array instead of
   the page table - removing the page lookup from the serial jump-address chain.
   the limit is the Memory(flat_max_words=...) parameter, else the FLIPJUMP_FLAT_MAX_WORDS
   environment variable, else this default. raising it trades startup time + footprint
   (the array is sentinel-filled: RSS = 8 bytes x span, fill ~0.1s/GB) for flat-path speed;
   the per-op cost is unaffected by the limit's value. */
#define FLAT_MAX_WORDS_DEFAULT (1ull << 23) /* 8M words, 64MB */
#define GARBAGE_SENTINEL (1ull << 63)
/* the w=64 flat gap-fill sentinel. w<=32 keeps the in-band bit-63 sentinel (values are
   masked below it, so it is exact), but w=64 values use all 64 bits - gaps are filled
   with this fixed magic value instead, and a real word that happens to equal it is
   disambiguated through the segment list on the cold path (gap words are never legally
   written, so in-segment + magic == real data). the constant is sqrt(3)'s fractional
   bits (the SHA-512 h1 constant) - any value works since collisions are handled; this
   one is just recognizable in memory dumps. */
#define FLAT_GARBAGE_MAGIC 0xBB67AE8584CAA73Bull

/* termination causes (mapped to TerminationCause in fjm_run.py) */
#define TERM_LOOPING 0
#define TERM_EOF 1
#define TERM_NULL_IP 2
#define TERM_MEMORY_ERROR 3

typedef struct {
    uint64_t* words;       /* PAGE_WORDS lazily-calloc'd words (masked to w bits) */
    uint64_t valid_start;  /* page-local fast-path valid range [valid_start, valid_end) */
    uint64_t valid_end;
} Page;

typedef struct {
    uint64_t key_plus1; /* page_index + 1; 0 marks an empty slot */
    Page* page;
} Slot;

typedef struct {
    uint64_t start; /* word address */
    uint64_t end;   /* word address (exclusive) */
} SegmentRange;

typedef struct {
    PyObject_HEAD

    int w;             /* memory width (8/16/32/64) */
    int ww;            /* log2(w) */
    uint64_t word_mask;
    int garbage_stop;  /* 1: out-of-segment access terminates; 0: reads 0 and continues */

    Slot* slots;       /* open-addressing page table */
    uint64_t slot_count; /* power of two */
    uint64_t slots_used;

    /* direct-mapped page cache (keyed by the page-index low bits): the ip/jump page and
       the wandering flip-target pages coexist instead of evicting each other. */
    uint64_t page_cache_key_plus1[16];
    Page* page_cache_page[16];
    /* parallel to the two above: the cached page's words pointer and fast valid range,
       so the run loop's hot path skips the Page* indirection (one less chained load) */
    uint64_t* page_cache_words[16];
    uint64_t page_cache_valid_start[16];
    uint64_t page_cache_valid_end[16];

    SegmentRange* segments; /* sorted, merged */
    Py_ssize_t segment_count;
    Py_ssize_t segment_capacity;
    int segments_sorted;

    /* flat-storage mode (built at the first run when eligible; NULL = paged mode) */
    uint64_t* flat;
    uint64_t flat_count;
    uint64_t flat_max_words; /* constructor override; 0 = use the env var / default */
    int storage_decided;
    int flat_covers_all; /* 1: every segment fits in the flat window ('flat'); 0 with a
                            non-NULL flat: low window flat, the rest paged ('hybrid') */

    int mem_error;            /* set when garbage_stop fired */
    uint64_t error_bit_address;

    /* jump-target speculation measurement (FLIPJUMP_MEASURE_SPECULATION=1):
       per executed op, would a "last jump target per ip" predictor have missed? */
    int spec_measured; /* did the last run measure? */
    unsigned long long spec_ops;
    unsigned long long spec_first;  /* first executions of an ip (no prediction yet) */
    unsigned long long spec_misses; /* jump word differed from its previous execution */

    unsigned long long last_run_op_count; /* op count of the last run (also on exceptions) */
    double last_run_paused_seconds;       /* IO-paused seconds of the last run (also on exceptions) */
} MemoryObject;

/* ---------------------------------------------------------------- pages */

static int mem_grow_slots(MemoryObject* m)
{
    uint64_t new_count = m->slot_count ? m->slot_count * 2 : 64;
    Slot* new_slots = (Slot*)calloc((size_t)new_count, sizeof(Slot));
    if (!new_slots) {
        PyErr_NoMemory();
        return -1;
    }
    for (uint64_t i = 0; i < m->slot_count; i++) {
        if (m->slots[i].key_plus1) {
            uint64_t h = (m->slots[i].key_plus1 * 0x9E3779B97F4A7C15ull) & (new_count - 1);
            while (new_slots[h].key_plus1) {
                h = (h + 1) & (new_count - 1);
            }
            new_slots[h] = m->slots[i];
        }
    }
    free(m->slots);
    m->slots = new_slots;
    m->slot_count = new_count;
    return 0;
}

static int segment_compare(const void* a, const void* b)
{
    const SegmentRange* sa = (const SegmentRange*)a;
    const SegmentRange* sb = (const SegmentRange*)b;
    if (sa->start < sb->start) return -1;
    if (sa->start > sb->start) return 1;
    return 0;
}

static void mem_ensure_segments_sorted(MemoryObject* m)
{
    if (!m->segments_sorted) {
        qsort(m->segments, (size_t)m->segment_count, sizeof(SegmentRange), segment_compare);
        m->segments_sorted = 1;
    }
}

/* is the word-address inside any segment? (binary search) */
static int word_is_valid(MemoryObject* m, uint64_t word_address)
{
    Py_ssize_t lo = 0, hi = m->segment_count - 1;
    mem_ensure_segments_sorted(m);
    while (lo <= hi) {
        Py_ssize_t mid = (lo + hi) / 2;
        if (word_address < m->segments[mid].start) {
            hi = mid - 1;
        } else if (word_address >= m->segments[mid].end) {
            lo = mid + 1;
        } else {
            return 1;
        }
    }
    return 0;
}

/* compute the page's fast-path valid range: the first segment-intersection with the page */
static void page_compute_validity(MemoryObject* m, uint64_t page_index, Page* page)
{
    uint64_t page_start = page_index << PAGE_BITS;
    uint64_t page_end = page_start + PAGE_WORDS;
    page->valid_start = 0;
    page->valid_end = 0;
    mem_ensure_segments_sorted(m);
    for (Py_ssize_t i = 0; i < m->segment_count; i++) {
        uint64_t s = m->segments[i].start, e = m->segments[i].end;
        if (e <= page_start || s >= page_end) {
            continue;
        }
        page->valid_start = (s > page_start) ? (s - page_start) : 0;
        page->valid_end = (e < page_end) ? (e - page_start) : PAGE_WORDS;
        return; /* additional intersections are handled by the slow word_is_valid path */
    }
}

static inline void page_cache_fill(MemoryObject* m, uint64_t cache_slot, uint64_t key, Page* page)
{
    m->page_cache_key_plus1[cache_slot] = key;
    m->page_cache_page[cache_slot] = page;
    m->page_cache_words[cache_slot] = page->words;
    m->page_cache_valid_start[cache_slot] = page->valid_start;
    m->page_cache_valid_end[cache_slot] = page->valid_end;
}

static Page* mem_get_page(MemoryObject* m, uint64_t page_index)
{
    uint64_t key = page_index + 1;
    uint64_t cache_slot = page_index & 15;
    uint64_t h;
    if (key == m->page_cache_key_plus1[cache_slot]) {
        return m->page_cache_page[cache_slot];
    }
    if (m->slots_used * 2 >= m->slot_count) {
        if (mem_grow_slots(m) < 0) {
            return NULL;
        }
    }
    h = (key * 0x9E3779B97F4A7C15ull) & (m->slot_count - 1);
    while (m->slots[h].key_plus1) {
        if (m->slots[h].key_plus1 == key) {
            page_cache_fill(m, cache_slot, key, m->slots[h].page);
            return m->slots[h].page;
        }
        h = (h + 1) & (m->slot_count - 1);
    }
    /* allocate a new page */
    {
        Page* page = (Page*)malloc(sizeof(Page));
        if (!page) {
            PyErr_NoMemory();
            return NULL;
        }
        page->words = (uint64_t*)calloc(PAGE_WORDS, sizeof(uint64_t));
        if (!page->words) {
            free(page);
            PyErr_NoMemory();
            return NULL;
        }
        page_compute_validity(m, page_index, page);
        m->slots[h].key_plus1 = key;
        m->slots[h].page = page;
        m->slots_used++;
        page_cache_fill(m, cache_slot, key, page);
        return page;
    }
}

/* validity check for the interpreted program's accesses.
   returns 1 if ok to access; 0 if the run must terminate with a memory error (sets m->mem_error),
   -1 on python error. in continue-mode invalid accesses are allowed (read 0 / write). */
static inline int access_check(MemoryObject* m, Page* page, uint64_t word_address)
{
    uint64_t off = word_address & PAGE_MASK;
    if (off >= page->valid_start && off < page->valid_end) {
        return 1;
    }
    if (word_is_valid(m, word_address)) {
        return 1;
    }
    if (!m->garbage_stop) {
        return 1;
    }
    m->mem_error = 1;
    m->error_bit_address = word_address << m->ww;
    return 0;
}

static inline int flat_garbage(MemoryObject* m, uint64_t word_address)
{
    if (!m->garbage_stop) {
        return 1; /* continue-mode reads 0 (flat mode requires garbage_stop, but be safe) */
    }
    m->mem_error = 1;
    m->error_bit_address = word_address << m->ww;
    return 0;
}

/* is this flat value the width's garbage sentinel? (w<=32: in-band bit 63, exact;
   w=64: the magic fill value - may collide with real data, see flat_garbage_check) */
static inline int flat_is_garbage(const MemoryObject* m, uint64_t value)
{
    return (m->w <= 32) ? ((value & GARBAGE_SENTINEL) != 0) : (value == FLAT_GARBAGE_MAGIC);
}

static inline int flat_seg_contains(const MemoryObject* m, uint64_t word_address)
{
    Py_ssize_t seg;
    for (seg = 0; seg < m->segment_count; seg++) {
        if (word_address >= m->segments[seg].start && word_address < m->segments[seg].end) {
            return 1;
        }
    }
    return 0;
}

/* a flat word whose value matched the width's sentinel: decide whether it is REAL
   garbage (an out-of-segment touch - reported via flat_garbage, returns -1) or, at
   w=64, an in-segment word that legitimately holds the magic value (kept, returns 0).
   w<=32 has no collisions, so a sentinel match there is always real garbage. */
static inline int flat_garbage_check(MemoryObject* m, uint64_t word_address, uint64_t* value)
{
    if (m->w > 32 && flat_seg_contains(m, word_address)) {
        return 0; /* the word really holds the magic constant - real data */
    }
    if (!flat_garbage(m, word_address)) {
        return -1;
    }
    *value = 0; /* continue-mode safety (flat requires garbage_stop, so unreachable) */
    return 0;
}

/* read the w-bit word at the word-address. returns 0 on success, -1 on stop (mem_error or
   python error - distinguish via m->mem_error). */
static inline int mem_read_word(MemoryObject* m, uint64_t word_address, uint64_t* out)
{
    Page* page;
    if (m->flat && word_address < m->flat_count) {
        uint64_t value = m->flat[word_address];
        if (flat_is_garbage(m, value) && flat_garbage_check(m, word_address, &value) < 0) {
            return -1;
        }
        *out = value;
        return 0;
    }
    /* paged, or a hybrid access above the flat window (validated by access_check) */
    page = mem_get_page(m, word_address >> PAGE_BITS);
    if (!page) {
        return -1;
    }
    if (!access_check(m, page, word_address)) {
        return -1;
    }
    *out = page->words[word_address & PAGE_MASK];
    return 0;
}

/* flip the bit at the bit-address. returns 0 on success, -1 on stop. */
static inline int mem_flip_bit(MemoryObject* m, uint64_t bit_address)
{
    uint64_t word_address = bit_address >> m->ww;
    Page* page;
    if (m->flat && word_address < m->flat_count) {
        uint64_t value = m->flat[word_address];
        if (flat_is_garbage(m, value) && flat_garbage_check(m, word_address, &value) < 0) {
            return -1;
        }
        m->flat[word_address] = value ^ (1ull << (bit_address & (uint64_t)(m->w - 1)));
        return 0;
    }
    /* paged, or a hybrid access above the flat window (validated by access_check) */
    page = mem_get_page(m, word_address >> PAGE_BITS);
    if (!page) {
        return -1;
    }
    if (!access_check(m, page, word_address)) {
        return -1;
    }
    page->words[word_address & PAGE_MASK] ^= 1ull << (bit_address & (uint64_t)(m->w - 1));
    return 0;
}

/* set/clear the bit at the bit-address. returns 0 on success, -1 on stop. */
static inline int mem_write_bit(MemoryObject* m, uint64_t bit_address, int bit_value)
{
    uint64_t word_address = bit_address >> m->ww;
    uint64_t bit = 1ull << (bit_address & (uint64_t)(m->w - 1));
    Page* page;
    if (m->flat && word_address < m->flat_count) {
        uint64_t value = m->flat[word_address];
        if (flat_is_garbage(m, value) && flat_garbage_check(m, word_address, &value) < 0) {
            return -1;
        }
        m->flat[word_address] = bit_value ? (value | bit) : (value & ~bit);
        return 0;
    }
    /* paged, or a hybrid access above the flat window (validated by access_check) */
    page = mem_get_page(m, word_address >> PAGE_BITS);
    if (!page) {
        return -1;
    }
    if (!access_check(m, page, word_address)) {
        return -1;
    }
    if (bit_value) {
        page->words[word_address & PAGE_MASK] |= bit;
    } else {
        page->words[word_address & PAGE_MASK] &= ~bit;
    }
    return 0;
}

/* read the (possibly unaligned) w-bit word at the bit-address. */
static inline int mem_get_word_unaligned(MemoryObject* m, uint64_t bit_address, uint64_t* out)
{
    uint64_t word_address = bit_address >> m->ww;
    uint64_t bit_offset = bit_address & (uint64_t)(m->w - 1);
    uint64_t lsw, msw;
    if (bit_offset == 0) {
        return mem_read_word(m, word_address, out);
    }
    if (word_address == m->word_mask) {
        /* an unaligned read whose high word would wrap past the top of the address space -
           the python reference terminates with a memory error here */
        m->mem_error = 1;
        m->error_bit_address = bit_address;
        return -1;
    }
    if (mem_read_word(m, word_address, &lsw) < 0) {
        return -1;
    }
    if (mem_read_word(m, word_address + 1, &msw) < 0) {
        return -1;
    }
    *out = ((lsw >> bit_offset) | (msw << (m->w - bit_offset))) & m->word_mask;
    return 0;
}

/* the effective flat-storage span limit: the constructor parameter, else the
   FLIPJUMP_FLAT_MAX_WORDS environment variable, else the built-in default. */
static uint64_t mem_flat_words_limit(MemoryObject* m)
{
    if (m->flat_max_words) {
        return m->flat_max_words;
    }
    {
        const char* env_limit = getenv("FLIPJUMP_FLAT_MAX_WORDS");
        if (env_limit && env_limit[0]) {
            uint64_t limit = strtoull(env_limit, NULL, 0);
            if (limit) {
                return limit;
            }
        }
    }
    return FLAT_MAX_WORDS_DEFAULT;
}

/* decide the storage mode (once, at the first run): in garbage-stop mode, the LOW WINDOW
   of memory - segment data below the flat-words limit - gets a dense flat array (gaps
   carry the in-band bit-63 sentinel at w<=32, the FLAT_GARBAGE_MAGIC fill at w=64).
   when every segment fits inside the window the mode is 'flat' (exactly the historic
   behavior); segments continuing or living above it stay page-backed and the mode is
   'hybrid' - the run loop reads sub-window words from the array and falls back to the
   paged helpers above it (FJ code lives low, so the hot op fetches stay flat even for
   sieve-style programs whose data tables sit at 1<<63). copies the already-loaded
   sub-window page data in; the pages are kept (see below). a failed/oversized flat allocation
   WARNS to stderr and falls back to paged (set FLIPJUMP_NO_FLAT=1 to opt into paged silently);
   but no segments, or no segment in the low window (the first op at address 0), RAISE - those
   are malformed programs, not resource limits. */
static int mem_decide_storage(MemoryObject* m)
{
    uint64_t max_end = 0, low_max_end = 0, limit, i;
    Py_ssize_t seg;
    if (m->storage_decided) {
        return 0;
    }
    m->storage_decided = 1;

    if (m->segment_count == 0) {
        PyErr_SetString(PyExc_ValueError,
                        "the program has no memory segments - a FlipJump program must define its "
                        "first op at address 0");
        return -1;
    }
    if (!m->garbage_stop) {
        return 0; /* continue-mode: paged (the flat sentinel scheme needs garbage-stop) */
    }
    {
        const char* no_flat = getenv("FLIPJUMP_NO_FLAT");
        if (no_flat && no_flat[0] == '1') {
            return 0; /* paged (forced - for A/B benchmarking and debugging) */
        }
    }
    limit = mem_flat_words_limit(m);
    for (seg = 0; seg < m->segment_count; seg++) {
        const uint64_t start = m->segments[seg].start, end = m->segments[seg].end;
        if (end > max_end) {
            max_end = end;
        }
        if (start < limit) {
            const uint64_t clamped_end = (end < limit) ? end : limit;
            if (clamped_end > low_max_end) {
                low_max_end = clamped_end;
            }
        }
    }
    if (low_max_end == 0) {
        /* every segment lies at or above the flat window - impossible for a valid program,
           whose first op occupies words 0..1 (a segment at address 0, far below the window) */
        PyErr_SetString(PyExc_ValueError,
                        "the program has no segment in the low address window - the first op at "
                        "address 0 is missing");
        return -1;
    }
    if (low_max_end > SIZE_MAX / sizeof(uint64_t)) {
        fprintf(stderr, "flipjump: flat-storage window is too large (byte size overflows); running paged (slower). lower --flat-max-words / "
                "FLIPJUMP_FLAT_MAX_WORDS, or set FLIPJUMP_NO_FLAT=1 to silence this.\n");
        fflush(stderr);
        return 0; /* paged fallback */
    }

    {
        /* deterministic allocation-failure injection for the error-path test
           (a real flat-array OOM is not safely reproducible across platforms) */
        const char* simulate_alloc_fail = getenv("FLIPJUMP_TEST_FLAT_ALLOC_FAIL");
        if (simulate_alloc_fail && simulate_alloc_fail[0] == '1') {
            fprintf(stderr, "flipjump: flat-storage allocation failed (test injection); running paged (slower). lower --flat-max-words / "
                "FLIPJUMP_FLAT_MAX_WORDS, or set FLIPJUMP_NO_FLAT=1 to silence this.\n");
            fflush(stderr);
            return 0; /* paged fallback */
        }
    }
    m->flat = (uint64_t*)malloc((size_t)low_max_end * sizeof(uint64_t));
    if (!m->flat) {
        fprintf(stderr, "flipjump: flat-storage allocation failed; running paged (slower). lower --flat-max-words / "
                "FLIPJUMP_FLAT_MAX_WORDS, or set FLIPJUMP_NO_FLAT=1 to silence this.\n");
        fflush(stderr);
        return 0; /* paged fallback - the program still runs, just slower */
    }
    m->flat_count = low_max_end;
    m->flat_covers_all = (max_end <= low_max_end);
    {
        const uint64_t garbage_fill = (m->w <= 32) ? GARBAGE_SENTINEL : FLAT_GARBAGE_MAGIC;
        for (i = 0; i < low_max_end; i++) {
            m->flat[i] = garbage_fill;
        }
    }
    for (seg = 0; seg < m->segment_count; seg++) {
        const uint64_t start = m->segments[seg].start;
        const uint64_t end_clamped =
            (m->segments[seg].end < low_max_end) ? m->segments[seg].end : low_max_end;
        if (start < end_clamped) {
            memset(m->flat + start, 0, (size_t)(end_clamped - start) * sizeof(uint64_t));
        }
    }
    /* copy the loaded in-segment data: each allocated page, intersected with each
       segment. the pages themselves are KEPT: out-of-segment memory (gaps between
       segments, addresses beyond the span) stays page-backed for the API/device
       accessors (set_word/get_word route by segment membership), so flat mode is
       device-transparent exactly like paged mode. the in-segment page copies go stale
       (the run loop updates flat only) but are never read again - the routing is
       strict. the program itself cannot touch out-of-segment words (garbage-stop). */
    if (m->slots) {
        for (i = 0; i < m->slot_count; i++) {
            uint64_t page_start, page_end;
            if (!m->slots[i].key_plus1) {
                continue;
            }
            page_start = (m->slots[i].key_plus1 - 1) << PAGE_BITS;
            page_end = page_start + PAGE_WORDS;
            for (seg = 0; seg < m->segment_count; seg++) {
                uint64_t lo = (m->segments[seg].start > page_start) ? m->segments[seg].start : page_start;
                uint64_t hi = (m->segments[seg].end < page_end) ? m->segments[seg].end : page_end;
                if (hi > low_max_end) {
                    hi = low_max_end; /* the part above the window stays page-backed */
                }
                if (lo < hi) {
                    memcpy(m->flat + lo, m->slots[i].page->words + (lo - page_start),
                           (size_t)(hi - lo) * sizeof(uint64_t));
                }
            }
        }
    }
    return 0;
}

/* ---------------------------------------------------------------- Memory type */

/* free every owned allocation (also safe on a fresh zeroed object) */
static void mem_free_allocations(MemoryObject* self)
{
    if (self->slots) {
        for (uint64_t i = 0; i < self->slot_count; i++) {
            if (self->slots[i].key_plus1) {
                free(self->slots[i].page->words);
                free(self->slots[i].page);
            }
        }
        free(self->slots);
        self->slots = NULL;
    }
    free(self->flat);
    self->flat = NULL;
    free(self->segments);
    self->segments = NULL;
}

static int Memory_init(PyObject* op, PyObject* args, PyObject* kwds)
{
    MemoryObject* self = (MemoryObject*)op;
    static char* kwlist[] = {"memory_width", "garbage_stop", "flat_max_words", NULL};
    int w, garbage_stop = 1;
    unsigned long long flat_max_words = 0; /* 0: use FLIPJUMP_FLAT_MAX_WORDS / the default */
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "i|pK", kwlist, &w, &garbage_stop, &flat_max_words)) {
        return -1;
    }
    if (w != 8 && w != 16 && w != 32 && w != 64) {
        PyErr_SetString(PyExc_ValueError, "memory_width must be 8, 16, 32 or 64");
        return -1;
    }
    mem_free_allocations(self); /* __init__ may be called again on a live object */
    self->w = w;
    self->ww = (w == 8) ? 3 : (w == 16) ? 4 : (w == 32) ? 5 : 6;
    self->word_mask = (w == 64) ? ~0ull : ((1ull << w) - 1);
    self->garbage_stop = garbage_stop;
    self->slots = NULL;
    self->slot_count = 0;
    self->slots_used = 0;
    memset(self->page_cache_key_plus1, 0, sizeof(self->page_cache_key_plus1));
    memset(self->page_cache_page, 0, sizeof(self->page_cache_page));
    memset(self->page_cache_words, 0, sizeof(self->page_cache_words));
    memset(self->page_cache_valid_start, 0, sizeof(self->page_cache_valid_start));
    memset(self->page_cache_valid_end, 0, sizeof(self->page_cache_valid_end));
    self->segments = NULL;
    self->segment_count = 0;
    self->segment_capacity = 0;
    self->segments_sorted = 1;
    self->flat = NULL;
    self->flat_count = 0;
    self->flat_max_words = flat_max_words;
    self->storage_decided = 0;
    self->flat_covers_all = 0;
    self->mem_error = 0;
    self->error_bit_address = 0;
    self->spec_measured = 0;
    self->spec_ops = 0;
    self->spec_first = 0;
    self->spec_misses = 0;
    self->last_run_op_count = 0;
    self->last_run_paused_seconds = 0.0;
    return 0;
}

static void Memory_dealloc(PyObject* op)
{
    MemoryObject* self = (MemoryObject*)op;
    PyTypeObject* type = Py_TYPE(op);
    freefunc tp_free;
    mem_free_allocations(self);
    tp_free = (freefunc)PyType_GetSlot(type, Py_tp_free);
    tp_free(op);
    Py_DECREF(type); /* heap types own a reference from their instances */
}

static PyObject* Memory_add_segment(MemoryObject* self, PyObject* args)
{
    unsigned long long start_word, length_words;
    if (!PyArg_ParseTuple(args, "KK", &start_word, &length_words)) {
        return NULL;
    }
    /* reject overflowing ranges - a wrapped end would corrupt the flat-array build
       (memset/memcpy with a wild size) and the validity binary-search invariants */
    if (start_word + length_words < start_word) {
        PyErr_SetString(PyExc_ValueError, "segment range overflows the 64-bit word-address space");
        return NULL;
    }
    if (self->segment_count == self->segment_capacity) {
        Py_ssize_t new_capacity = self->segment_capacity ? self->segment_capacity * 2 : 8;
        SegmentRange* new_segments = (SegmentRange*)realloc(self->segments, (size_t)new_capacity * sizeof(SegmentRange));
        if (!new_segments) {
            return PyErr_NoMemory();
        }
        self->segments = new_segments;
        self->segment_capacity = new_capacity;
    }
    self->segments[self->segment_count].start = start_word;
    self->segments[self->segment_count].end = start_word + length_words;
    self->segment_count++;
    self->segments_sorted = 0;
    /* validity ranges of already-allocated pages may change - recompute them */
    if (self->slots) {
        for (uint64_t i = 0; i < self->slot_count; i++) {
            if (self->slots[i].key_plus1) {
                page_compute_validity(self, self->slots[i].key_plus1 - 1, self->slots[i].page);
            }
        }
    }
    Py_RETURN_NONE;
}

static PyObject* Memory_set_word(MemoryObject* self, PyObject* args)
{
    unsigned long long word_address, value;
    Page* page;
    if (!PyArg_ParseTuple(args, "KK", &word_address, &value)) {
        return NULL;
    }
    if (self->flat && word_address < self->flat_count && flat_seg_contains(self, word_address)) {
        self->flat[word_address] = value & self->word_mask;
        Py_RETURN_NONE;
    }
    /* out-of-segment (or paged mode): page-backed - device/API memory beyond the declared
       segments behaves exactly as in paged mode (the program itself cannot touch it under
       garbage-stop, so the flat array and the pages never alias). */
    page = mem_get_page(self, word_address >> PAGE_BITS);
    if (!page) {
        return NULL;
    }
    page->words[word_address & PAGE_MASK] = value & self->word_mask;
    Py_RETURN_NONE;
}

static PyObject* Memory_get_word(MemoryObject* self, PyObject* args)
{
    unsigned long long word_address;
    Page* page;
    if (!PyArg_ParseTuple(args, "K", &word_address)) {
        return NULL;
    }
    if (self->flat && word_address < self->flat_count && flat_seg_contains(self, word_address)) {
        return PyLong_FromUnsignedLongLong(self->flat[word_address]);
    }
    /* out-of-segment (or paged mode): page-backed - see set_word */
    page = mem_get_page(self, word_address >> PAGE_BITS);
    if (!page) {
        return NULL;
    }
    return PyLong_FromUnsignedLongLong(page->words[word_address & PAGE_MASK]);
}

/* bulk-load: set_words(start_word_address, values_list) */
static PyObject* Memory_set_words(MemoryObject* self, PyObject* args)
{
    unsigned long long start_word;
    PyObject* values;
    Py_ssize_t count, i;
    if (!PyArg_ParseTuple(args, "KO", &start_word, &values)) {
        return NULL;
    }
    count = PySequence_Size(values);
    if (count < 0) {
        return NULL;
    }
    if (start_word + (uint64_t)count < start_word) {
        PyErr_SetString(PyExc_ValueError, "set_words range overflows the 64-bit word-address space");
        return NULL;
    }
    if (self->flat && start_word + (uint64_t)count > self->flat_count) {
        PyErr_SetString(PyExc_ValueError, "set_words address is beyond the flat-storage span");
        return NULL;
    }
    Py_INCREF(values);
    for (i = 0; i < count; i++) {
        unsigned long long value;
        Page* page;
        PyObject* item = PySequence_GetItem(values, i);
        if (!item) {
            Py_DECREF(values);
            return NULL;
        }
        value = PyLong_AsUnsignedLongLong(item);
        Py_DECREF(item);
        if (value == (unsigned long long)-1 && PyErr_Occurred()) {
            Py_DECREF(values);
            return NULL;
        }
        if (self->flat) {
            self->flat[start_word + i] = value & self->word_mask;
            continue;
        }
        page = mem_get_page(self, (start_word + i) >> PAGE_BITS);
        if (!page) {
            Py_DECREF(values);
            return NULL;
        }
        page->words[(start_word + i) & PAGE_MASK] = value & self->word_mask;
    }
    Py_DECREF(values);
    Py_RETURN_NONE;
}

#define CAUSE_PYTHON_ERROR (-2)

/* ------------------------------------------------ speculation measurement

   measures the would-be miss-rate of jump-target speculation: remember the last jump
   word per executed ip; an execution whose jump word differs from the previous execution
   of the same ip is a miss. enabled with FLIPJUMP_MEASURE_SPECULATION=1 - it runs a
   dedicated slow reference-style loop (works for flat AND paged storage, any width), so
   the normal run-loops' hot paths are completely untouched. */

typedef struct {
    uint64_t ip_plus1; /* ip + 1; 0 marks an empty slot */
    uint64_t jump_word;
} SpecSlot;

typedef struct {
    SpecSlot* slots; /* open-addressing, power-of-two sized */
    uint64_t slot_count;
    uint64_t slots_used;
} SpecShadow;

static int spec_grow(SpecShadow* shadow)
{
    uint64_t new_count = shadow->slot_count ? shadow->slot_count * 2 : (1ull << 16);
    SpecSlot* new_slots = (SpecSlot*)calloc((size_t)new_count, sizeof(SpecSlot));
    uint64_t i;
    if (!new_slots) {
        PyErr_NoMemory();
        return -1;
    }
    for (i = 0; i < shadow->slot_count; i++) {
        if (shadow->slots[i].ip_plus1) {
            uint64_t h = (shadow->slots[i].ip_plus1 * 0x9E3779B97F4A7C15ull) & (new_count - 1);
            while (new_slots[h].ip_plus1) {
                h = (h + 1) & (new_count - 1);
            }
            new_slots[h] = shadow->slots[i];
        }
    }
    free(shadow->slots);
    shadow->slots = new_slots;
    shadow->slot_count = new_count;
    return 0;
}

/* record one executed (ip, jump_word): bumps spec_first or spec_misses. -1 on alloc fail. */
static int spec_record(SpecShadow* shadow, MemoryObject* m, uint64_t ip, uint64_t jump_word)
{
    uint64_t key = ip + 1, h;
    if (shadow->slots_used * 2 >= shadow->slot_count) {
        if (spec_grow(shadow) < 0) {
            return -1;
        }
    }
    h = (key * 0x9E3779B97F4A7C15ull) & (shadow->slot_count - 1);
    while (shadow->slots[h].ip_plus1) {
        if (shadow->slots[h].ip_plus1 == key) {
            if (shadow->slots[h].jump_word != jump_word) {
                m->spec_misses++;
                shadow->slots[h].jump_word = jump_word;
            }
            return 0;
        }
        h = (h + 1) & (shadow->slot_count - 1);
    }
    shadow->slots[h].ip_plus1 = key;
    shadow->slots[h].jump_word = jump_word;
    shadow->slots_used++;
    m->spec_first++;
    return 0;
}

/* the measurement run-loop: the reference op order through the generic mem_* helpers
   (slow - measurement only). returns the termination cause / CAUSE_PYTHON_ERROR. */
static int run_measured_loop(MemoryObject* self, PyObject* read_bit, PyObject* write_bit,
                             PyObject* eof_exception_type, uint64_t start_ip, uint64_t* ops_out,
                             double* paused_seconds_out)
{
    const uint64_t width = (uint64_t)self->w;
    const uint64_t ww = (uint64_t)self->ww;
    const uint64_t dw = 2 * width;
    const uint64_t out1 = dw + 1;
    const uint64_t in_addr = 3 * width + ww + 1; /* 3w + #w */
    const uint64_t in_lo_exclusive = in_addr - dw;

    SpecShadow shadow = {NULL, 0, 0};
    uint64_t ip = start_ip, ops = 0;
    int cause = CAUSE_PYTHON_ERROR;

    self->spec_ops = 0;
    self->spec_first = 0;
    self->spec_misses = 0;
    self->mem_error = 0;

    for (;;) {
        uint64_t f, j;

        if ((ops & SIGNAL_CHECK_MASK) == SIGNAL_CHECK_MASK) {
            self->last_run_op_count = ops;
            if (PyErr_CheckSignals() < 0) {
                goto done;
            }
        }

        /* read flip word */
        if (mem_get_word_unaligned(self, ip, &f) < 0) {
            goto memory_error;
        }

        /* handle output */
        if (f <= out1 && f >= dw) {
            PyObject* result = PyObject_CallFunctionObjArgs(write_bit, (f == out1) ? Py_True : Py_False, NULL);
            if (!result) {
                goto done;
            }
            Py_DECREF(result);
        }

        /* handle input */
        if (ip <= in_addr && ip > in_lo_exclusive) {
            PyObject* result;
            int bit_value;
            double io_start = monotonic_seconds();
            result = PyObject_CallNoArgs(read_bit);
            *paused_seconds_out += monotonic_seconds() - io_start;
            if (!result) {
                if (PyErr_ExceptionMatches(eof_exception_type)) {
                    PyErr_Clear();
                    cause = TERM_EOF;
                    goto done;
                }
                goto done;
            }
            bit_value = PyObject_IsTrue(result);
            Py_DECREF(result);
            if (bit_value < 0) {
                goto done;
            }
            if (mem_write_bit(self, in_addr, bit_value) < 0) {
                goto memory_error;
            }
        }

        /* FLIP! */
        if (mem_flip_bit(self, f) < 0) {
            goto memory_error;
        }

        /* read jump word (after the flip - the flip may modify it) */
        if (mem_get_word_unaligned(self, ip + width, &j) < 0) {
            goto memory_error;
        }
        ops++;

        if (spec_record(&shadow, self, ip, j) < 0) {
            goto done;
        }

        /* check finish? */
        if (j == ip && !(f >= ip && f - ip < dw)) {
            cause = TERM_LOOPING;
            goto done;
        }
        if (j < dw) {
            cause = TERM_NULL_IP;
            goto done;
        }

        /* JUMP! */
        ip = j;
    }

memory_error:
    if (self->mem_error) {
        self->mem_error = 0;
        cause = TERM_MEMORY_ERROR;
    }
done:
    free(shadow.slots);
    self->spec_ops = ops;
    self->last_run_op_count = ops;
    self->last_run_paused_seconds = *paused_seconds_out;
    *ops_out = ops;
    return cause;
}

/* the dedicated flat-storage run loop - the common fast case (no last-ops ring, flat
   memory): all paged-mode and ring branches are out of the per-op path.

   shaped for the hot path:
   - every rare condition (unaligned ip, out-of-span words, garbage sentinels, IO hits,
     termination) is a forward goto to a cold block after the main body, so the common op
     runs straight-line with all conditional branches fall-through;
   - the two IO range tests fold to one unsigned compare each;
   - the signal check is strip-mined into an outer loop (the inner do-while back-edge is
     a fused dec-jnz), keeping the per-op signal cost at one decrement;
   - width/ww arrive as literals from run_flat_loop's dispatch, so the shifts and the
     IO-range constants are immediates - freeing enough registers that the loop's live
     values stop spilling to the stack.
   returns the termination cause, or CAUSE_PYTHON_ERROR with the python error set. */
static FJ_ALWAYS_INLINE int run_flat_loop_impl(MemoryObject* self, PyObject* read_bit, PyObject* write_bit,
                                               PyObject* eof_exception_type, uint64_t start_ip, uint64_t* ops_out,
                                               double* paused_seconds_out, const uint64_t width, const uint64_t ww)
{
    const uint64_t bit_mask = width - 1;
    const uint64_t dw = 2 * width;
    const uint64_t in_addr = 3 * width + ww + 1; /* 3w + #w */
    const uint64_t in_lo_exclusive = in_addr - dw;
    uint64_t* const flat = self->flat;
    const uint64_t flat_count = self->flat_count;

    uint64_t ip = start_ip, ops = 0;
    uint64_t word_address, f, flip_word_address, flip_value, j;
    uint64_t cold_word; /* out-param for the cold-path reads, so f/j stay in registers */
    uint64_t inner_left;
    int cause = CAUSE_PYTHON_ERROR;

    self->mem_error = 0;
    for (;;) {
        /* the signal check is strip-mined out of the per-op path: the inner do-while
           runs SIGNAL_CHECK_MASK+1 ops on a fused dec-jnz back-edge, the outer loop
           checks signals - same cadence as a per-op (ops & MASK) == MASK test. */
        self->last_run_op_count = ops;
        if (PyErr_CheckSignals() < 0) {
            goto done;
        }
        inner_left = SIGNAL_CHECK_MASK + 1;
        do {
            /* read flip word */
            if (ip & bit_mask) {
                goto cold_unaligned_flip_word;
            }
            word_address = ip >> ww;
            if (word_address + 1 >= flat_count) {
                goto cold_flip_word_out_of_span;
            }
            f = flat[word_address];
            if (width <= 32 ? ((f & GARBAGE_SENTINEL) != 0) : (f == FLAT_GARBAGE_MAGIC)) {
                goto cold_flip_word_garbage;
            }
        flip_word_ready:

            /* IO - one unsigned compare each: output when f is dw or dw+1;
               input when in_lo_exclusive < ip <= in_addr */
            if (f - dw <= 1) {
                goto cold_output;
            }
        after_output:
            if (ip - in_lo_exclusive - 1 < dw) {
                goto cold_input;
            }
        after_input:

            /* FLIP! */
            flip_word_address = f >> ww;
            if (flip_word_address >= flat_count) {
                goto cold_flip_out_of_span;
            }
            flip_value = flat[flip_word_address];
            if (width <= 32 ? ((flip_value & GARBAGE_SENTINEL) != 0) : (flip_value == FLAT_GARBAGE_MAGIC)) {
                goto cold_flip_garbage;
            }
        flip_value_ready:
            flat[flip_word_address] = flip_value ^ (1ull << (f & bit_mask));
        after_flip:

            /* read jump word (after the flip - the flip may modify it). word_address is
               (uint64_t)-2 for unaligned ops, so they take the slow read too. */
            if (word_address + 1 >= flat_count) {
                goto cold_jump_word_slow;
            }
            j = flat[word_address + 1];
            if (width <= 32 ? ((j & GARBAGE_SENTINEL) != 0) : (j == FLAT_GARBAGE_MAGIC)) {
                goto cold_jump_word_garbage;
            }
        jump_word_ready:
            ops++;

            /* check finish? */
            if (j == ip) {
                goto cold_maybe_looping;
            }
        not_looping:
            if (j < dw) {
                cause = TERM_NULL_IP;
                goto done;
            }

            /* JUMP! */
            ip = j;
        } while (--inner_left);
        continue;

        /* ------- cold blocks: rare per-op work, off the straight-line path. they sit
           outside the do-while and goto back into it (labels are function-scope) so the
           hot path's only taken branch is the dec-jnz back-edge. */

    cold_unaligned_flip_word:
        if (mem_get_word_unaligned(self, ip, &cold_word) < 0) {
            goto memory_error;
        }
        f = cold_word;
        word_address = (uint64_t)-2; /* the jump word is read the slow way below */
        goto flip_word_ready;

    cold_flip_word_out_of_span:
        /* the op's words reach past the flat window: read per word through the routing
           helper (hybrid: page-backed far code; pure flat: the paged access checks
           report the out-of-segment error) */
        if (mem_read_word(self, word_address, &cold_word) < 0) {
            goto memory_error;
        }
        f = cold_word;
        goto flip_word_ready;

    cold_flip_word_garbage: /* w=64: possibly real data equal to the magic fill */
        if (flat_garbage_check(self, word_address, &f) < 0) {
            goto memory_error;
        }
        goto flip_word_ready;

    cold_output:
    {
        PyObject* result = PyObject_CallFunctionObjArgs(write_bit, (f == dw + 1) ? Py_True : Py_False, NULL);
        if (!result) {
            goto done;
        }
        Py_DECREF(result);
        goto after_output;
    }

    cold_input:
    {
        PyObject* result;
        int bit_value;
        double io_start = monotonic_seconds();
        result = PyObject_CallNoArgs(read_bit);
        *paused_seconds_out += monotonic_seconds() - io_start;
        if (!result) {
            if (PyErr_ExceptionMatches(eof_exception_type)) {
                PyErr_Clear();
                cause = TERM_EOF;
                goto done;
            }
            goto done;
        }
        bit_value = PyObject_IsTrue(result);
        Py_DECREF(result);
        if (bit_value < 0) {
            goto done;
        }
        if (mem_write_bit(self, in_addr, bit_value) < 0) {
            goto memory_error;
        }
        goto after_input;
    }

    cold_flip_out_of_span:
        /* a flip above the flat window: the routing helper flips page-backed far data
           (hybrid - e.g. sieve's table) or reports the out-of-segment error */
        if (mem_flip_bit(self, f) < 0) {
            goto memory_error;
        }
        goto after_flip;

    cold_flip_garbage: /* w=64: possibly real data equal to the magic fill */
        if (flat_garbage_check(self, flip_word_address, &flip_value) < 0) {
            goto memory_error;
        }
        goto flip_value_ready;

    cold_jump_word_slow:
        if (mem_get_word_unaligned(self, ip + width, &cold_word) < 0) {
            goto memory_error;
        }
        j = cold_word;
        goto jump_word_ready;

    cold_jump_word_garbage: /* w=64: possibly real data equal to the magic fill */
        if (flat_garbage_check(self, word_address + 1, &j) < 0) {
            goto memory_error;
        }
        goto jump_word_ready;

    cold_maybe_looping:
        if (f >= ip && f - ip < dw) {
            goto not_looping; /* the op flips its own words - not a halt */
        }
        cause = TERM_LOOPING;
        goto done;
    }

memory_error:
    if (self->mem_error) {
        self->mem_error = 0;
        cause = TERM_MEMORY_ERROR;
    }
done:
    self->last_run_op_count = ops;
    self->last_run_paused_seconds = *paused_seconds_out;
    *ops_out = ops;
    return cause;
}

/* dispatch with literal width/ww (the supported power-of-two widths) so the
   force-inlined body constant-folds; unusual widths keep the generic body. */
static int run_flat_loop(MemoryObject* self, PyObject* read_bit, PyObject* write_bit, PyObject* eof_exception_type,
                         uint64_t start_ip, uint64_t* ops_out, double* paused_seconds_out)
{
    switch (self->w) {
        case 64:
            return run_flat_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                      paused_seconds_out, 64, 6);
        case 32:
            return run_flat_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                      paused_seconds_out, 32, 5);
        case 16:
            return run_flat_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                      paused_seconds_out, 16, 4);
        case 8:
            return run_flat_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                      paused_seconds_out, 8, 3);
        default:
            return run_flat_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                      paused_seconds_out, (uint64_t)self->w, (uint64_t)self->ww);
    }
}

/* the generic run loop - the paged path (plus the rare flat-with-last-ops-ring debug
   mode), reshaped like run_flat_loop_impl: rare conditions are forward gotos to cold
   blocks (the hot path is straight-line, all conditionals fall-through), the IO range
   tests fold to one unsigned compare each, the signal check is strip-mined, and
   width/ww arrive as literals from run_generic_loop's dispatch.

   the hot lane is an aligned non-straddling op whose page sits in the 16-way cache with
   the words inside the fast valid range - read via the widened cache (words pointer +
   valid range cached next to the key), skipping the Page* indirection. the op's two
   words share one cached slot; the flip inlines the same cache-hit fast path and falls
   back to mem_flip_bit (cold) for everything else. ops outside a page's fast valid
   range take the full helpers (mem_read_word validates per word) - exact original
   semantics. with_ring is literal 0 on the common clones, folding away the ring write
   and the flat sub-lane (flat storage reaches this loop only with a ring - the
   debugger's last-ops mode).
   returns the termination cause, or CAUSE_PYTHON_ERROR with the python error set. */
static FJ_ALWAYS_INLINE int run_paged_loop_impl(MemoryObject* self, PyObject* read_bit, PyObject* write_bit,
                                                PyObject* eof_exception_type, uint64_t start_ip, uint64_t* ops_out,
                                                double* paused_seconds_out, uint64_t* last_ops_ring,
                                                Py_ssize_t last_ops_length, uint64_t* ring_writes_out,
                                                const uint64_t width, const uint64_t ww, const int with_ring)
{
    const uint64_t bit_mask = width - 1;
    const uint64_t dw = 2 * width;
    const uint64_t in_addr = 3 * width + ww + 1; /* 3w + #w */
    const uint64_t in_lo_exclusive = in_addr - dw;
    uint64_t* const flat = self->flat; /* non-NULL only in the with_ring clone */
    const uint64_t flat_count = self->flat_count;

    uint64_t ip = start_ip, ops = 0, ring_writes = 0;
    uint64_t word_address, op_offset, op_slot, f, j;
    uint64_t* op_words; /* the hot lane's cached words; NULL marks the slow lanes */
    uint64_t* op_flat_jump = NULL;
    uint64_t cold_word; /* out-param for the cold-path reads, so f/j stay in registers */
    uint64_t inner_left;
    int cause = CAUSE_PYTHON_ERROR;

    self->mem_error = 0;
    self->last_run_op_count = 0;
    for (;;) {
        self->last_run_op_count = ops;
        if (PyErr_CheckSignals() < 0) {
            goto loop_done; /* python error - cause stays CAUSE_PYTHON_ERROR */
        }
        inner_left = SIGNAL_CHECK_MASK + 1;
        do {
            if (with_ring) {
                last_ops_ring[ring_writes % (uint64_t)last_ops_length] = ip;
                ring_writes++;
                op_flat_jump = NULL;
                if (flat) {
                    goto flat_lane;
                }
            }

            /* read flip word - the op's two words share one cached page slot */
            if (ip & bit_mask) {
                goto cold_unaligned_flip_word;
            }
            word_address = ip >> ww;
            op_offset = word_address & PAGE_MASK;
            if (op_offset == PAGE_MASK) {
                goto cold_straddling_op;
            }
            op_slot = (word_address >> PAGE_BITS) & 15;
            if ((word_address >> PAGE_BITS) + 1 != self->page_cache_key_plus1[op_slot]) {
                goto cold_op_page_miss;
            }
        op_page_cached:
            if (op_offset < self->page_cache_valid_start[op_slot] ||
                op_offset >= self->page_cache_valid_end[op_slot]) {
                goto cold_op_slow;
            }
            op_words = self->page_cache_words[op_slot];
            f = op_words[op_offset];
        flip_word_ready:

            /* IO - one unsigned compare each: output when f is dw or dw+1;
               input when in_lo_exclusive < ip <= in_addr */
            if (f - dw <= 1) {
                goto cold_output;
            }
        after_output:
            if (ip - in_lo_exclusive - 1 < dw) {
                goto cold_input;
            }
        after_input:

            /* FLIP - the cache-hit valid-range fast path inline; the rest falls back to
               mem_flip_bit (page miss, outside the valid range, flat - all cold) */
            if (with_ring) {
                goto cold_flip_slow; /* the debug clone keeps the helper (handles flat) */
            }
            {
                const uint64_t flip_word_address = f >> ww;
                const uint64_t flip_offset = flip_word_address & PAGE_MASK;
                const uint64_t flip_slot = (flip_word_address >> PAGE_BITS) & 15;
                if ((flip_word_address >> PAGE_BITS) + 1 != self->page_cache_key_plus1[flip_slot]) {
                    goto cold_flip_slow;
                }
                if (flip_offset < self->page_cache_valid_start[flip_slot] ||
                    flip_offset >= self->page_cache_valid_end[flip_slot]) {
                    goto cold_flip_slow;
                }
                self->page_cache_words[flip_slot][flip_offset] ^= 1ull << (f & bit_mask);
            }
        after_flip:

            /* read jump word (after the flip - the flip may modify it, including this
               word). the hot lane re-reads through the cached slot: op_offset >= the
               valid start already held for f, so only the end bound needs checking. */
            if (with_ring && op_flat_jump) {
                j = *op_flat_jump;
                if (flat_is_garbage(self, j) && flat_garbage_check(self, (uint64_t)(op_flat_jump - flat), &j) < 0) {
                    goto memory_or_python_error;
                }
            } else if (op_words) {
                if (op_offset + 1 >= self->page_cache_valid_end[op_slot]) {
                    goto cold_jump_word_slow;
                }
                j = op_words[op_offset + 1];
            } else if (ip & bit_mask) {
                if (mem_get_word_unaligned(self, ip + width, &cold_word) < 0) {
                    goto memory_or_python_error;
                }
                j = cold_word;
            } else {
                if (mem_read_word(self, (ip >> ww) + 1, &cold_word) < 0) {
                    goto memory_or_python_error;
                }
                j = cold_word;
            }
        jump_word_ready:
            ops++;

            /* check finish? */
            if (j == ip) {
                goto cold_maybe_looping;
            }
        not_looping:
            if (j < dw) {
                cause = TERM_NULL_IP;
                goto loop_done;
            }

            /* JUMP! */
            ip = j;
        } while (--inner_left);
        continue;

        /* ------- cold blocks: rare per-op work, off the straight-line path (they sit
           outside the do-while and goto back into it - labels are function-scope) */

    flat_lane: /* with_ring && flat: the debugger's last-ops mode on flat storage */
        op_words = NULL;
        if (ip & bit_mask) {
            goto cold_unaligned_flip_word;
        }
        word_address = ip >> ww;
        if (word_address + 1 >= flat_count) {
            if (mem_read_word(self, word_address, &cold_word) < 0) {
                goto memory_or_python_error;
            }
            f = cold_word;
            goto flip_word_ready;
        }
        f = flat[word_address];
        if (flat_is_garbage(self, f) && flat_garbage_check(self, word_address, &f) < 0) {
            goto memory_or_python_error;
        }
        op_flat_jump = flat + word_address + 1;
        goto flip_word_ready;

    cold_unaligned_flip_word:
        op_words = NULL;
        if (mem_get_word_unaligned(self, ip, &cold_word) < 0) {
            goto memory_or_python_error;
        }
        f = cold_word;
        goto flip_word_ready;

    cold_straddling_op: /* the op's words straddle a page boundary - per-word helpers */
        op_words = NULL;
        if (mem_read_word(self, word_address, &cold_word) < 0) {
            goto memory_or_python_error;
        }
        f = cold_word;
        goto flip_word_ready;

    cold_op_page_miss:
        if (!mem_get_page(self, word_address >> PAGE_BITS)) {
            goto memory_or_python_error;
        }
        goto op_page_cached; /* mem_get_page filled slot op_slot */

    cold_op_slow: /* in-page but outside the fast valid range - mem_read_word validates
                     per word (and the slow jump read below does too) */
        op_words = NULL;
        if (mem_read_word(self, word_address, &cold_word) < 0) {
            goto memory_or_python_error;
        }
        f = cold_word;
        goto flip_word_ready;

    cold_output:
    {
        PyObject* result = PyObject_CallFunctionObjArgs(write_bit, (f == dw + 1) ? Py_True : Py_False, NULL);
        if (!result) {
            goto loop_done;
        }
        Py_DECREF(result);
        goto after_output;
    }

    cold_input:
    {
        PyObject* result;
        int bit_value;
        double io_start = monotonic_seconds();
        result = PyObject_CallNoArgs(read_bit);
        *paused_seconds_out += monotonic_seconds() - io_start;
        if (!result) {
            if (PyErr_ExceptionMatches(eof_exception_type)) {
                PyErr_Clear();
                cause = TERM_EOF;
                goto loop_done;
            }
            goto loop_done;
        }
        bit_value = PyObject_IsTrue(result);
        Py_DECREF(result);
        if (bit_value < 0) {
            goto loop_done;
        }
        if (mem_write_bit(self, in_addr, bit_value) < 0) {
            goto memory_or_python_error;
        }
        goto after_input;
    }

    cold_flip_slow:
        if (mem_flip_bit(self, f) < 0) {
            goto memory_or_python_error;
        }
        goto after_flip;

    cold_jump_word_slow: /* hot op whose jump word lies past the fast valid range */
        if (mem_read_word(self, (ip >> ww) + 1, &cold_word) < 0) {
            goto memory_or_python_error;
        }
        j = cold_word;
        goto jump_word_ready;

    cold_maybe_looping:
        if (f >= ip && f - ip < dw) {
            goto not_looping; /* the op flips its own words - not a halt */
        }
        cause = TERM_LOOPING;
        goto loop_done;
    }

memory_or_python_error:
    if (self->mem_error) {
        self->mem_error = 0;
        cause = TERM_MEMORY_ERROR;
    }
loop_done:
    self->last_run_op_count = ops;
    self->last_run_paused_seconds = *paused_seconds_out;
    *ops_out = ops;
    *ring_writes_out = ring_writes;
    return cause;
}

/* dispatch with literal width/ww and the ring flag, mirroring run_flat_loop: the
   no-ring clones fold away the ring write and the flat sub-lane. */
static int run_generic_loop(MemoryObject* self, PyObject* read_bit, PyObject* write_bit,
                            PyObject* eof_exception_type, uint64_t start_ip, uint64_t* ops_out,
                            double* paused_seconds_out, uint64_t* last_ops_ring, Py_ssize_t last_ops_length,
                            uint64_t* ring_writes_out)
{
    if (last_ops_ring) {
        return run_paged_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                   paused_seconds_out, last_ops_ring, last_ops_length, ring_writes_out,
                                   (uint64_t)self->w, (uint64_t)self->ww, 1);
    }
    switch (self->w) {
        case 64:
            return run_paged_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                       paused_seconds_out, NULL, 0, ring_writes_out, 64, 6, 0);
        case 32:
            return run_paged_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                       paused_seconds_out, NULL, 0, ring_writes_out, 32, 5, 0);
        case 16:
            return run_paged_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                       paused_seconds_out, NULL, 0, ring_writes_out, 16, 4, 0);
        case 8:
            return run_paged_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                       paused_seconds_out, NULL, 0, ring_writes_out, 8, 3, 0);
        default:
            return run_paged_loop_impl(self, read_bit, write_bit, eof_exception_type, start_ip, ops_out,
                                       paused_seconds_out, NULL, 0, ring_writes_out, (uint64_t)self->w,
                                       (uint64_t)self->ww, 0);
    }
}


/* build the (cause, op_count, error_bit_address_or_None, last_ops, paused_seconds) result tuple.
   takes ownership of last_ops_ring (frees it). */
static PyObject* build_run_result(MemoryObject* self, int cause, uint64_t ops, uint64_t* last_ops_ring,
                                  Py_ssize_t last_ops_length, uint64_t ring_writes, double paused_seconds)
{
    PyObject* last_ops_list = PyList_New(0);
    PyObject* error_address;
    if (!last_ops_list) {
        free(last_ops_ring);
        return NULL;
    }
    (void)ops;
    if (last_ops_ring) {
        /* emit in execution order: oldest first */
        uint64_t total = (ring_writes < (uint64_t)last_ops_length) ? ring_writes : (uint64_t)last_ops_length;
        uint64_t ring_pos = ring_writes % (uint64_t)last_ops_length;
        uint64_t start = (ring_pos + (uint64_t)last_ops_length - total) % (uint64_t)last_ops_length;
        for (uint64_t i = 0; i < total; i++) {
            PyObject* address = PyLong_FromUnsignedLongLong(last_ops_ring[(start + i) % (uint64_t)last_ops_length]);
            if (!address || PyList_Append(last_ops_list, address) < 0) {
                Py_XDECREF(address);
                Py_DECREF(last_ops_list);
                free(last_ops_ring);
                return NULL;
            }
            Py_DECREF(address);
        }
        free(last_ops_ring);
    }
    if (cause == TERM_MEMORY_ERROR) {
        error_address = PyLong_FromUnsignedLongLong(self->error_bit_address);
    } else {
        error_address = Py_None;
        Py_INCREF(Py_None);
    }
    if (!error_address) {
        Py_DECREF(last_ops_list);
        return NULL;
    }
    return Py_BuildValue("iKNNd", cause, (unsigned long long)ops, error_address, last_ops_list, paused_seconds);
}

/* the run loop.
   run(read_bit, write_bit, eof_exception_type, last_ops_length=0, start_ip=0)
   -> (termination_cause, op_count, error_bit_address_or_None, last_ops_list, paused_seconds) */
static PyObject* Memory_run(MemoryObject* self, PyObject* args, PyObject* kwds)
{
    static char* kwlist[] = {"read_bit", "write_bit", "eof_exception_type", "last_ops_length", "start_ip", NULL};
    PyObject* read_bit;
    PyObject* write_bit;
    PyObject* eof_exception_type;
    Py_ssize_t last_ops_length = 0;
    unsigned long long start_ip = 0;

    uint64_t* last_ops_ring = NULL;

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "OOO|nK", kwlist, &read_bit, &write_bit, &eof_exception_type,
                                     &last_ops_length, &start_ip)) {
        return NULL;
    }
    if (last_ops_length < 0) {
        last_ops_length = 0;
    }

    if (mem_decide_storage(self) < 0) {
        return NULL;
    }

    self->spec_measured = 0;
    {
        const char* measure_speculation = getenv("FLIPJUMP_MEASURE_SPECULATION");
        if (measure_speculation && measure_speculation[0] == '1' && last_ops_length == 0) {
            uint64_t measured_ops = 0;
            double measured_paused = 0.0;
            int measured_cause = run_measured_loop(self, read_bit, write_bit, eof_exception_type, start_ip,
                                                   &measured_ops, &measured_paused);
            if (measured_cause == CAUSE_PYTHON_ERROR) {
                return NULL;
            }
            self->spec_measured = 1;
            return build_run_result(self, measured_cause, measured_ops, NULL, 0, 0, measured_paused);
        }
    }

    if (self->flat && last_ops_length == 0) {
        /* the common fast case gets the dedicated loop (no ring, no paged branches) */
        uint64_t fast_ops = 0;
        double fast_paused = 0.0;
        int fast_cause =
            run_flat_loop(self, read_bit, write_bit, eof_exception_type, start_ip, &fast_ops, &fast_paused);
        if (fast_cause == CAUSE_PYTHON_ERROR) {
            return NULL;
        }
        return build_run_result(self, fast_cause, fast_ops, NULL, 0, 0, fast_paused);
    }

    if (last_ops_length > 0) {
        last_ops_ring = (uint64_t*)calloc((size_t)last_ops_length, sizeof(uint64_t));
        if (!last_ops_ring) {
            return PyErr_NoMemory();
        }
    }

    {
        uint64_t loop_ops = 0, loop_ring_writes = 0;
        double loop_paused = 0.0;
        int loop_cause = run_generic_loop(self, read_bit, write_bit, eof_exception_type, start_ip, &loop_ops,
                                          &loop_paused, last_ops_ring, last_ops_length, &loop_ring_writes);
        if (loop_cause == CAUSE_PYTHON_ERROR) {
            free(last_ops_ring);
            return NULL;
        }
        return build_run_result(self, loop_cause, loop_ops, last_ops_ring, last_ops_length, loop_ring_writes,
                                loop_paused);
    }
}

static PyObject* Memory_get_op_count(MemoryObject* self, void* closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(self->last_run_op_count);
}

static PyObject* Memory_get_paused_seconds(MemoryObject* self, void* closure)
{
    (void)closure;
    return PyFloat_FromDouble(self->last_run_paused_seconds);
}

static PyObject* Memory_get_storage_mode(MemoryObject* self, void* closure)
{
    (void)closure;
    if (!self->storage_decided) {
        Py_RETURN_NONE;
    }
    return PyUnicode_FromString(self->flat ? (self->flat_covers_all ? "flat" : "hybrid") : "paged");
}

static PyObject* Memory_get_speculation_stats(MemoryObject* self, void* closure)
{
    (void)closure;
    if (!self->spec_measured) {
        Py_RETURN_NONE;
    }
    return Py_BuildValue("{s:K, s:K, s:K}", "ops", self->spec_ops, "first_executions", self->spec_first, "misses",
                         self->spec_misses);
}

static PyObject* Memory_get_allocated_bytes(MemoryObject* self, void* closure)
{
    (void)closure;
    return PyLong_FromUnsignedLongLong(self->flat_count * sizeof(uint64_t) +
                                       self->slots_used * PAGE_WORDS * sizeof(uint64_t) +
                                       self->slot_count * sizeof(Slot));
}

/* ---- CPU calibration: an L1-resident throughput micro-benchmark, same toolchain as the engine
 * the denominator for normalizing fj/s across the wheel targets: a slower (or throttled) runner
 * lowers BOTH this and fj/s proportionally, so the fj/C ratio isolates a genuine per-arch engine
 * regression from a merely-slow cpu. for that to hold the yardstick must share the engine's
 * BOTTLENECK, not just "be C": the native run-loop is bound by L1 + execution-port throughput on
 * an L1-resident hot region (3 loads + 1 RMW store per op, branchless). this loop mirrors that -
 * a 16 KB working set (stays in L1 on every target), four independent lanes (saturate the load/
 * store ports, throughput- not latency-bound), no data-dependent branches. (an earlier streaming
 * prime-sieve yardstick was DRAM-bandwidth + branch-prediction bound, so its cpu ranking diverged
 * from the engine's - e.g. Apple Silicon's huge bandwidth inflated it while Graviton's deflated
 * it, swinging the ratio 0.10-0.30 across arches.) the loop carries a real cross-iteration data
 * dependency and writes the buffer back, so it can't be elided; `checksum` is deterministic for a
 * given `iterations` (pure uint64 arithmetic, endianness-independent) and is pinned by the caller. */
#define CALIB_WORDS 2048u             /* 16 KB working set - L1-resident on every wheel target */
#define CALIB_MASK (CALIB_WORDS - 1)
#define CALIB_LANE_OPS 4              /* memory-op "units" per iteration (the four lanes) */

static PyObject* fjcore_cpu_calibrate(PyObject* module, PyObject* args)
{
    (void)module;
    unsigned long long iterations = 0;
    if (!PyArg_ParseTuple(args, "K", &iterations)) {
        return NULL;
    }
    if (iterations < 1) {
        PyErr_SetString(PyExc_ValueError, "cpu_calibrate: iterations must be >= 1");
        return NULL;
    }

    uint64_t* buf = (uint64_t*)malloc(CALIB_WORDS * sizeof(uint64_t));
    if (!buf) {
        return PyErr_NoMemory();
    }
    for (unsigned w = 0; w < CALIB_WORDS; w++) {
        buf[w] = 0x9E3779B97F4A7C15ull * (uint64_t)(w + 1); /* nonzero, varied seed */
    }

    uint64_t checksum = 0;
    double seconds = 0.0;

    Py_BEGIN_ALLOW_THREADS
    double t0 = monotonic_seconds();
    /* four independent accumulator lanes over four non-overlapping 512-word stripes: each lane
       does load + rotate-mix + RMW store, and the lanes have no inter-dependency so out-of-order
       execution fills the L1 load/store ports (throughput-bound, like the engine). */
    uint64_t a = 0x0123456789ABCDEFull;
    uint64_t b = 0x123456789ABCDEF0ull;
    uint64_t c = 0x23456789ABCDEF01ull;
    uint64_t d = 0x3456789ABCDEF012ull;
    for (unsigned long long i = 0; i < iterations; i++) {
        uint64_t i0 = i & CALIB_MASK;
        uint64_t i1 = (i0 + (CALIB_WORDS / 4)) & CALIB_MASK;
        uint64_t i2 = (i0 + (CALIB_WORDS / 2)) & CALIB_MASK;
        uint64_t i3 = (i0 + 3 * (CALIB_WORDS / 4)) & CALIB_MASK;
        a += buf[i0] ^ ((a << 13) | (a >> 51));
        b += buf[i1] ^ ((b << 13) | (b >> 51));
        c += buf[i2] ^ ((c << 13) | (c >> 51));
        d += buf[i3] ^ ((d << 13) | (d >> 51));
        buf[i0] = a;
        buf[i1] = b;
        buf[i2] = c;
        buf[i3] = d;
    }
    seconds = monotonic_seconds() - t0;
    for (unsigned w = 0; w < CALIB_WORDS; w++) {
        checksum ^= buf[w];
    }
    checksum ^= a ^ b ^ c ^ d;
    Py_END_ALLOW_THREADS

    free(buf);

    return Py_BuildValue("{s:K,s:K,s:K,s:d}",
                         "iterations", (unsigned long long)iterations,
                         "ops", (unsigned long long)(iterations * CALIB_LANE_OPS),
                         "checksum", (unsigned long long)checksum,
                         "seconds", seconds);
}

static PyMethodDef module_methods[] = {
    {"cpu_calibrate", (PyCFunction)fjcore_cpu_calibrate, METH_VARARGS,
     "cpu_calibrate(iterations) -> dict(iterations, ops, checksum, seconds)\n"
     "an L1-resident load+RMW throughput micro-benchmark compiled with the same toolchain\n"
     "as the engine and sharing its bottleneck; ops/seconds is the platform's CPU yardstick\n"
     "for normalizing fj/s. checksum is deterministic for a given iterations count."},
    {NULL, NULL, 0, NULL},
};

static PyMethodDef Memory_methods[] = {
    {"add_segment", (PyCFunction)Memory_add_segment, METH_VARARGS,
     "add_segment(start_word_address, length_words) - declare a valid memory segment"},
    {"set_word", (PyCFunction)Memory_set_word, METH_VARARGS, "set_word(word_address, value)"},
    {"get_word", (PyCFunction)Memory_get_word, METH_VARARGS, "get_word(word_address) -> value"},
    {"set_words", (PyCFunction)Memory_set_words, METH_VARARGS, "set_words(start_word_address, values)"},
    {"run", (PyCFunction)Memory_run, METH_VARARGS | METH_KEYWORDS,
     "run(read_bit, write_bit, eof_exception_type, last_ops_length=0, start_ip=0)\n"
     "-> (termination_cause, op_count, error_bit_address_or_None, last_ops, paused_seconds)"},
    {NULL, NULL, 0, NULL},
};

static PyGetSetDef Memory_getset[] = {
    {"last_run_op_count", (getter)Memory_get_op_count, NULL, "op count of the last run (valid on exceptions too)",
     NULL},
    {"last_run_paused_seconds", (getter)Memory_get_paused_seconds, NULL,
     "IO-paused seconds of the last run (valid on exceptions too)", NULL},
    {"allocated_bytes", (getter)Memory_get_allocated_bytes, NULL,
     "bytes allocated for memory pages (footprint scales with touched memory, not segment sizes)", NULL},
    {"storage_mode", (getter)Memory_get_storage_mode, NULL,
     "'flat'/'hybrid'/'paged' - the storage mode chosen at the first run (None before it)", NULL},
    {"speculation_stats", (getter)Memory_get_speculation_stats, NULL,
     "dict(ops, first_executions, misses) of the last FLIPJUMP_MEASURE_SPECULATION=1 run, else None", NULL},
    {NULL, NULL, NULL, NULL, NULL},
};

static PyType_Slot Memory_type_slots[] = {
    {Py_tp_dealloc, (void*)Memory_dealloc},
    {Py_tp_doc, (void*)"segment-aware FlipJump memory (flat/paged) + native run-loop"},
    {Py_tp_methods, (void*)Memory_methods},
    {Py_tp_getset, (void*)Memory_getset},
    {Py_tp_init, (void*)Memory_init},
    {Py_tp_new, (void*)PyType_GenericNew},
    {0, NULL},
};

static PyType_Spec Memory_type_spec = {
    "_fjcore.Memory",        /* name */
    sizeof(MemoryObject),    /* basicsize */
    0,                       /* itemsize */
    Py_TPFLAGS_DEFAULT,      /* flags */
    Memory_type_slots,       /* slots */
};

static PyModuleDef fjcore_module = {
    PyModuleDef_HEAD_INIT, "_fjcore", "native FlipJump interpreter engine", -1, module_methods, NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC PyInit__fjcore(void)
{
    PyObject* module;
    PyObject* memory_type = PyType_FromSpec(&Memory_type_spec);
    if (!memory_type) {
        return NULL;
    }
    module = PyModule_Create(&fjcore_module);
    if (!module) {
        Py_DECREF(memory_type);
        return NULL;
    }
    if (PyModule_AddObject(module, "Memory", memory_type) < 0) {
        Py_DECREF(memory_type);
        Py_DECREF(module);
        return NULL;
    }
    PyModule_AddIntConstant(module, "TERM_LOOPING", TERM_LOOPING);
    PyModule_AddIntConstant(module, "TERM_EOF", TERM_EOF);
    PyModule_AddIntConstant(module, "TERM_NULL_IP", TERM_NULL_IP);
    PyModule_AddIntConstant(module, "TERM_MEMORY_ERROR", TERM_MEMORY_ERROR);
    PyModule_AddObject(module, "FLAT_GARBAGE_MAGIC", PyLong_FromUnsignedLongLong(FLAT_GARBAGE_MAGIC));
    return module;
}
