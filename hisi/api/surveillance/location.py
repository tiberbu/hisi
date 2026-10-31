import json
import math
from datetime import datetime, timezone

import frappe

from hisi.api.response_handler import BadRequest

FRESH_WINDOW_S = 120
JUMP_SPEED_KM_H = 5
EARTH_RADIUS_M = 6371008.8


class CheckinResult:
	def __init__(self, status, distance_m, flags):
		self.status = status
		self.distance_m = distance_m
		self.flags = flags


def haversine_m(a_lat, a_lng, b_lat, b_lng):
	lat1, lng1, lat2, lng2 = (math.radians(v) for v in (a_lat, a_lng, b_lat, b_lng))
	dlat = lat2 - lat1
	dlng = lng2 - lng1
	arc = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
	return 2 * math.asin(min(1.0, math.sqrt(arc))) * EARTH_RADIUS_M


def resolve_radius_km(mandate_radius_km, facility_radius_km, settings_radius_km):
	for value in (mandate_radius_km, facility_radius_km, settings_radius_km):
		if value is None:
			continue
		try:
			radius = float(value)
		except (TypeError, ValueError):
			raise BadRequest("The geofence radius is misconfigured.")
		if radius < 0:
			raise BadRequest("The geofence radius must be a positive number.")
		if radius == 0:
			# Float columns are NOT NULL DEFAULT 0.0; 0.0 is the unset sentinel
			continue
		return radius
	raise BadRequest("HISI Settings surveillance_default_radius_km is not configured.")


def fix_age_seconds(ts):
	if not ts:
		return None
	try:
		fix_time = frappe.utils.get_datetime(ts)
	except (TypeError, ValueError):
		return None
	if fix_time is None:
		return None
	try:
		if fix_time.tzinfo is not None:
			# client ts may carry a UTC offset; compare both in naive UTC
			fix_time = fix_time.astimezone(timezone.utc).replace(tzinfo=None)
			now = datetime.now(timezone.utc).replace(tzinfo=None)
		else:
			now = frappe.utils.now_datetime()
		return (now - fix_time).total_seconds()
	except TypeError:
		return None


def evaluate_checkin(location, centre_lat, centre_lng, radius_km, max_accuracy_m):
	if location in (None, ""):
		return CheckinResult("unreported", None, ["unreported"])
	if isinstance(location, str):
		try:
			location = json.loads(location)
		except ValueError:
			return CheckinResult("unreported", None, ["unreported"])
	if not isinstance(location, dict):
		return CheckinResult("unreported", None, ["unreported"])
	try:
		lat = float(location.get("lat"))
		lng = float(location.get("lng"))
	except (TypeError, ValueError):
		return CheckinResult("unreported", None, ["unreported"])
	distance_m = round(haversine_m(lat, lng, centre_lat, centre_lng), 2)
	if location.get("is_mock") in (True, 1, "true", "True"):
		return CheckinResult("spoofed", distance_m, ["spoofed"])
	age_seconds = fix_age_seconds(location.get("ts"))
	if age_seconds is None or age_seconds > FRESH_WINDOW_S:
		return CheckinResult("out_of_range", distance_m, ["out_of_range", "stale_timestamp"])
	accuracy = location.get("accuracy_m")
	try:
		accuracy = float(accuracy) if accuracy is not None else None
	except (TypeError, ValueError):
		accuracy = None
	if accuracy is not None and max_accuracy_m is not None and accuracy > float(max_accuracy_m):
		return CheckinResult("out_of_range", distance_m, ["out_of_range", "low_accuracy"])
	if distance_m > float(radius_km) * 1000:
		return CheckinResult("out_of_range", distance_m, ["out_of_range"])
	return CheckinResult("verified", distance_m, [])


def impossible_move(prev_location, current_location):
	if prev_location in (None, "") or not isinstance(prev_location, dict):
		return False
	if not current_location or not isinstance(current_location, dict):
		return False
	if not prev_location.get("ts") or not current_location.get("ts"):
		return False
	try:
		prev_ts = frappe.utils.get_datetime(prev_location["ts"])
		cur_ts = frappe.utils.get_datetime(current_location["ts"])
		prev_lat = float(prev_location.get("lat"))
		prev_lng = float(prev_location.get("lng"))
		cur_lat = float(current_location.get("lat"))
		cur_lng = float(current_location.get("lng"))
		delta_seconds = (cur_ts - prev_ts).total_seconds()
	except (TypeError, ValueError):
		return False
	distance = haversine_m(prev_lat, prev_lng, cur_lat, cur_lng)
	if delta_seconds <= 0:
		return distance > 1.0
	return (distance / delta_seconds) * 3.6 > JUMP_SPEED_KM_H
