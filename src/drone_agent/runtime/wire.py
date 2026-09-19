"""Presence-preserving protobuf conversion with domain validation at ingress.

保留字段存在性的 protobuf 转换；接收端重新进行领域校验。
"""

from __future__ import annotations

import json
from datetime import timezone
from enum import Enum

from google.protobuf.message import Message
from pydantic import BaseModel


def encode(model: BaseModel | dict, message: Message) -> Message:
    data = model.model_dump(mode="python") if isinstance(model, BaseModel) else model
    for name, value in data.items():
        if value is None:
            continue
        field = message.DESCRIPTOR.fields_by_name[name]
        if field.message_type and field.message_type.full_name == "google.protobuf.Timestamp":
            getattr(message, name).FromDatetime(value)
        elif field.message_type:
            if field.is_repeated:
                for item in value:
                    encode(item, getattr(message, name).add())
            else:
                encode(value, getattr(message, name))
        else:

            def scalar(item):
                if field.enum_type:
                    raw = item.name if isinstance(item, Enum) else str(item).upper()
                    prefix = field.enum_type.values[0].name.removesuffix("UNSPECIFIED")
                    return field.enum_type.values_by_name[prefix + raw].number
                if field.type == field.TYPE_BYTES:
                    return json.dumps(item, ensure_ascii=False, allow_nan=False).encode()
                return item

            if field.is_repeated:
                getattr(message, name).extend(scalar(item) for item in value)
            else:
                setattr(message, name, scalar(value))
    return message


def decode(message: Message) -> dict:
    if "schema_version" in message.DESCRIPTOR.fields_by_name:
        if not message.HasField("schema_version") or message.schema_version != "0.1.0":
            raise ValueError("unsupported or missing runtime schema version")
    result = {}
    for field, value in message.ListFields():

        def scalar(item):
            if field.message_type:
                if field.message_type.full_name == "google.protobuf.Timestamp":
                    return item.ToDatetime(tzinfo=timezone.utc)
                return decode(item)
            if field.enum_type:
                if item == 0:
                    raise ValueError("unspecified enum cannot authorize a runtime action")
                prefix = field.enum_type.values[0].name.removesuffix("UNSPECIFIED")
                return field.enum_type.values_by_number[item].name.removeprefix(prefix).lower()
            if field.type == field.TYPE_BYTES:
                return json.loads(item)
            return item

        result[field.name] = [scalar(item) for item in value] if field.is_repeated else scalar(value)
    return result
