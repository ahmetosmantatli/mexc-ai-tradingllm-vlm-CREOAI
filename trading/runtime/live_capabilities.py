"""Shared execution capabilities, selected by the opening runner."""
STRUCTURAL_IOC_RUNNERS = frozenset(('runner2', 'runner3'))
LIVE_RUNNERS = STRUCTURAL_IOC_RUNNERS | {'baseline-v1'}


def management_runner(opening_runner, selected_runner):
    """Resolve management independently from the immutable opening runner."""
    return selected_runner if selected_runner in LIVE_RUNNERS else opening_runner
