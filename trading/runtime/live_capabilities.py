"""Shared execution capabilities, selected by the opening runner."""
STRUCTURAL_IOC_RUNNERS = frozenset(('runner2', 'runner3'))


def management_runner(opening_runner, selected_runner):
    """Let Runner 3 manage Runner 2 exposure without changing ownership."""
    if opening_runner == 'runner2' and selected_runner == 'runner3':
        return 'runner3'
    return opening_runner
