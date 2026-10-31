from hisi.api.response_handler import APIResponse


class MandateInvalid(APIResponse):
	http_status_code = 400
	error_code = "MANDATE_INVALID"


class LocationCheckFailed(APIResponse):
	http_status_code = 400
	error_code = "LOCATION_CHECK_FAILED"


class MandateRevoked(APIResponse):
	http_status_code = 403
	error_code = "MANDATE_REVOKED"
