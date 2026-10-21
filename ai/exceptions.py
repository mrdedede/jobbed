class NoActiveCv(ValueError):
    """The user has no active CV to rewrite."""


class NoAnalysisForJob(RuntimeError):
    """The user has no analysis for the given job."""


class UnusableAnswer(RuntimeError):
    """The model answered, but not with JSON of the expected shape."""
