class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class DraftOutcomeUnknown(Exception):
    def __init__(self, draft_id: str | None = None):
        self.draft_id = draft_id
        super().__init__("Gmail write outcome needs reconciliation.")
