from ape.exceptions import ProviderError, SubprocessError


class GanacheProviderError(ProviderError):
    """
    An error related to the Ganache network provider plugin.
    """


class GanacheSubprocessError(GanacheProviderError, SubprocessError):
    """
    An error related to launching subprocesses to run Ganache.
    """


class GanacheNotInstalledError(GanacheSubprocessError):
    """
    Raised when Ganache is not installed.
    """

    def __init__(self):
        super().__init__(
            "Missing local Ganache NPM package. See ape-ganache README for install steps."
        )
