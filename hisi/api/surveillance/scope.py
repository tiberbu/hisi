def patient_identifiers(lines):
	ids = []
	for line in lines:
		if line["line_type"] == "patient" and line["patient_identifier"]:
			ids.append(str(line["patient_identifier"]).strip())
	return ids


def visit_ids(lines):
	ids = []
	for line in lines:
		if line["line_type"] == "visit" and line["visit_id"]:
			ids.append(str(line["visit_id"]).strip())
	return ids


def requested_identifier(filters):
	if not filters:
		return None
	identification_number = str(filters.get("identification_number") or "").strip()
	if identification_number:
		return identification_number
	value = str(filters.get("cr_id") or filters.get("client") or "").strip()
	return value or None


def requested_visit_id(filters):
	if not filters:
		return None
	value = str(filters.get("visit_id") or "").strip()
	return value or None


def patient_in_scope(lines, identifier):
	if not identifier:
		return False
	return identifier in patient_identifiers(lines)


def visit_in_scope(lines, visit_id):
	# an explicit visit filter is only checked when the mandate carries visit
	# lines; consent-derived visits (no filter) follow the patient scope
	if not visit_id:
		return True
	ids = visit_ids(lines)
	if not ids:
		return True
	return visit_id in ids
