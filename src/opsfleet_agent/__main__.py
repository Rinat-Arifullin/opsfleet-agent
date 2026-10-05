import sys


def _run() -> int:
    """``python -m opsfleet_agent``: a Ctrl-C even during the (slow) imports exits 130
    without a traceback; ``main`` handles everything after that."""
    try:
        from opsfleet_agent.cli import main

        return main()
    except KeyboardInterrupt:
        return 130


sys.exit(_run())
