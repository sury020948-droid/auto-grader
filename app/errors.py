class AppError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class ParseError(AppError):
    def __init__(self, detail: str):
        super().__init__(422, detail)


class GeminiUnavailableError(AppError):
    def __init__(self, detail: str | None = None):
        super().__init__(
            503,
            detail
            or "Gemini API 키가 설정되지 않았습니다. GEMINI_API_KEY를 설정하거나"
            " '텍스트 붙여넣기' 탭을 이용해 주세요.",
        )


class GeminiResponseError(AppError):
    def __init__(self, detail: str | None = None):
        super().__init__(
            502,
            detail
            or "Gemini Vision 처리 중 오류가 발생했습니다. 잠시 후 다시 시도하거나"
            " '텍스트 붙여넣기' 탭을 이용해 주세요.",
        )
