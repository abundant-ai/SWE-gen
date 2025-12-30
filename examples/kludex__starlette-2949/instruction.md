Type checkers currently reject exception handlers that declare a specific Exception subclass (and async handlers) as their exception parameter. For example, a handler like:

from starlette.applications import Request, Starlette
from starlette.responses import JSONResponse

class MyException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        self.extra = "extra"

def my_exception_handler(request: Request, exc: MyException) -> JSONResponse:
    return JSONResponse({"detail": exc.message, "extra": exc.extra}, status_code=400)

app = Starlette(debug=True, routes=[])
app.add_exception_handler(MyException, my_exception_handler)

does not pass static type checks because the handler Callable is not accepted for a parameter typed as a handler for Exception subtypes. Registering handlers via a mapping also fails to type-check. Expected behavior: handlers that declare a concrete Exception subclass (and async variants) should be accepted by type checkers when registered as exception handlers, without requiring untyped fallbacks.