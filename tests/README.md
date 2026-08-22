# tests

The suite for this repository. No framework, no dependencies — Python 3 and `bash`, same as
everything else here.

```bash
./tests/run.sh              # static checks + every fast test   (~1 min)
./tests/run.sh --static     # shellcheck and syntax only        (~2 s)
./tests/run.sh --full       # also containers and a live capture (~5 min)
./tests/run.sh harden       # only the tests whose name matches
```

## Why a pseudo-terminal

Most of this repository is interactive, and interactive code only misbehaves on a **real**
terminal: raw mode, escape sequences, the alternate screen. Piping stdin proves nothing there.

The bug that motivated all of this is a good example. A cursor key arrives as three bytes,
`ESC [ A`. netwatch 1.0 read the first byte through `sys.stdin` — a *buffered* stream, whose
`read(1)` pulls the whole sequence into Python's buffer while `select()` still reports the file
descriptor as empty. The rest of the sequence became invisible, the key was decoded as a bare
Escape, and Escape means "leave the menu": pressing Down quit the program. Every non-interactive
check passed.

So `lib.py` runs each program on an actual pty, types real key codes at it, and reads what it
paints — including the terminal's *other* arrow encoding (`ESC O A`, application cursor mode) and
two sequences delivered in a single read, which is what a held-down key actually produces.

## What is covered

| File | Covers |
|---|---|
| `test_discovery.py` | Script discovery, `# toolkit-*` metadata parsing, the fallback for scripts with none, launcher+engine deduplication, and the system check's verdicts against known-good and known-bad machines. |
| `test_launcher_tui.py` | Every launcher screen: browser, summary, options, docs pager, system info, help, filter — plus arrow keys, Escape and a clean exit. |
| `test_launcher_run.py` | `--list`, `--check`, `--run`, unknown names, and the one-click path end to end: select → summary → run → return. |
| `test_netwatch_tui.py` | netwatch's menu, the arrow-key regression, editing a setting (raw mode → prompt → raw mode), and with `--full` the live dashboard and `q`. |
| `test_netwatch_analysis.py` | The analysis, fed a synthesised dual-WAN capture with a known answer: four failovers, per-uplink loss and latency, the outages they caused, the verdict, the report and its charts. |
| `test_harden.py` | The audit is read-only and never prompts; the dry run touches nothing (verified by stat); and with `--full`, apply/rollback in a container, including the anti-lockout rule that password login stays enabled when no SSH key exists. |

`run.sh` also runs `shellcheck` on every `.sh`, `bash -n` on every `.sh`, `pyflakes` (or a syntax
check when it is not installed) on every `.py`, and asserts that every script in the repository
carries the metadata the launcher reads.

## Writing a test

```python
from lib import REPO, Suite, Term

s = Suite("what this covers")
with Term(["bash", "some-script.sh"], cwd=REPO) as t:
    s.check("it starts", t.expect("Main menu", 20))
    t.send("down")
    s.check("arrows move", t.selection() != first)
    t.send("q")
    s.check("it quits", t.wait(10) and t.exit_code == 0)
sys.exit(s.finish())
```

`expect()` waits for text and remembers where it matched, so consecutive expectations never lose
output to whichever read happened to swallow it. `selection()` returns the row the TUI is
highlighting. Anything slow, networked or destructive belongs behind
`os.environ.get("TOOLKIT_TEST_FULL") == "1"`.
