"""Employee Attendance & Analytics API.

Core endpoints: employee management, punch-in/out, and attendance listing.
All instants cross the HTTP boundary as epoch milliseconds and are stored as
UTC BSON datetimes. Calendar dates and shift times remain strings.
"""

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=3000,
    tz_aware=True,
    tzinfo=UTC,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    """Indexes are idempotent and also protect writes under concurrency."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_punch_lookup")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_date_status")


# Helpers ------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def epoch_to_datetime(value: int | None) -> datetime:
    if value is None:
        return utc_now()
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamps must be epoch milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def datetime_to_epoch(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    return parsed


def shift_instant(attendance_day: date, shift_time: str, overnight_next_day: bool = False) -> datetime:
    day = attendance_day + timedelta(days=1 if overnight_next_day else 0)
    h, m = map(int, shift_time.split(":"))
    return datetime.combine(day, time(h, m), IST).astimezone(UTC)


def attendance_date(punch_in: datetime, employee: dict) -> str:
    local = as_utc(punch_in).astimezone(IST)
    shift_start = employee["shift_start"]
    shift_end = employee["shift_end"]
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        local -= timedelta(days=1)
    return local.date().isoformat()


def late_minutes(punch_in: datetime, shift_start: str, day_string: str) -> int:
    day = parse_date(day_string)
    actual = as_utc(punch_in)
    start = shift_instant(day, shift_start)
    elapsed = int((actual - start).total_seconds() // 60)
    # The grace boundary is strict: exactly ten minutes late is not late.
    return elapsed if (actual - start).total_seconds() > 600 else 0


def overtime_minutes(punch_out: datetime, shift_end: str, day_string: str, shift_start: str) -> int:
    day = parse_date(day_string)
    overnight = shift_end <= shift_start
    end = shift_instant(day, shift_end, overnight_next_day=overnight)
    elapsed = int((as_utc(punch_out) - end).total_seconds() // 60)
    return elapsed if elapsed >= 30 else 0


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((as_utc(punch_out) - as_utc(punch_in)).total_seconds()))
    return float((seconds / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def public_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch(doc["created_at"]),
    }


def public_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": datetime_to_epoch(doc.get("punch_in")),
        "punch_out": datetime_to_epoch(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
        "history": [{
            "at": datetime_to_epoch(entry["at"]), "by": entry["by"], "reason": entry["reason"],
            "changes": {
                key: {"from": datetime_to_epoch(change["from"]) if key in {"punch_in", "punch_out"} else change["from"],
                      "to": datetime_to_epoch(change["to"]) if key in {"punch_in", "punch_out"} else change["to"]}
                for key, change in entry.get("changes", {}).items()
            },
        } for entry in doc.get("history", [])],
    }


def validate_pagination(page: int, page_size: int) -> None:
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(422, "page_size must be between 1 and 100")


# Request models ------------------------------------------------------------
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: int | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and (isinstance(value, bool) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: int | None = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and (isinstance(value, bool) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("punched_at must be epoch milliseconds")
        return value


# System and employees -------------------------------------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = utc_now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return public_employee(doc)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_employee(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


# Attendance -----------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    punched_at = epoch_to_datetime(body.punched_at)
    day_string = attendance_date(punched_at, employee)
    doc = {
        "emp_code": body.emp_code, "date": day_string, "status": body.status,
        "punch_in": punched_at, "punch_out": None, "work_hours": None,
        "late_minutes": late_minutes(punched_at, employee["shift_start"], day_string),
        "overtime_minutes": 0, "half_day": False, "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_to_datetime(body.punched_at)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record already punched out")
    punch_in_time = as_utc(record["punch_in"])
    elapsed = (punched_at - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    hours = work_hours(punch_in_time, punched_at)
    updates = {
        "punch_out": punched_at, "work_hours": hours,
        "overtime_minutes": overtime_minutes(punched_at, employee["shift_end"], record["date"], employee["shift_start"]),
        "half_day": hours < 4.50,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None}, {"$set": updates}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "attendance record already punched out")
    return public_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None:
        parse_date(date_from)
    if date_to is not None:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_attendance(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}
