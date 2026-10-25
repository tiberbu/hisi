import functools
import frappe

def dump(obj):
    if isinstance(obj, (list, tuple)):
        return [dump(o) for o in obj]
    return obj

class APIResponse(frappe.ValidationError):
    http_status_code = 400
    error_code = "API_ERROR"

    def __init__(self, message="", errors=None):
        super().__init__(message)
        self.message = message or self.__class__.__name__
        self.errors = errors or []

    def respond(self):
        frappe.local.response["http_status_code"] = self.http_status_code
        frappe.local.response.update({
            "status": "error",
            "code": self.error_code,
            "message": self.message,
            "errors": self.errors,
        })

    @staticmethod
    def respond_with(model, status=200):
        data = model.model_dump(mode="json") if hasattr(model, "model_dump") else dump(model)
        frappe.local.response["http_status_code"] = status
        frappe.local.response.update(data if isinstance(data, dict) else {"data": data})

    @staticmethod
    def success(data=None, status=200, **extra):
        frappe.local.response["http_status_code"] = status
        frappe.local.response.update({"status": "success", "data": dump(data), **extra})
class BadRequest(APIResponse):
    http_status_code = 400
    error_code = "BAD_REQUEST"
class Unauthorized(APIResponse):
    http_status_code = 401
    error_code = "UNAUTHORIZED"
class Forbidden(APIResponse):
    http_status_code = 403
    error_code = "FORBIDDEN"
class NotFound(APIResponse):
    http_status_code = 404
    error_code = "NOT_FOUND"
class Conflict(APIResponse):
    http_status_code = 409
    error_code = "CONFLICT"
class UpstreamError(APIResponse):
    http_status_code = 400
    error_code = "UPSTREAM_ERROR"
def api_endpoint(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except APIResponse as e:
            e.respond()
        except frappe.PermissionError:
            Forbidden("You do not have access to this resource").respond()
        except Exception:
            frappe.log_error(fn.__name__, frappe.get_traceback())
            frappe.local.response["http_status_code"] = 500
            frappe.local.response.update({
                "status": "error",
                "code": "INTERNAL_ERROR",
                "message": "Internal server error",
                "errors": [],
            })
    return wrapper
