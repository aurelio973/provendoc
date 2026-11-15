import base64
import math
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from enum import Enum


def _convert_key(key):
    """
    Convert special YAML/Python keys into strings with proper YAML
    semantics.
    :param key:
    :return:
    """

    # YAML null
    if key is None:
        return "null"

    # YAML true
    elif key is True:
        return "true"

    # YAML false
    elif key is False:
        return "false"

    return str(key)

def safe_serialize(obj,visited_ids=None):
    """
    Safe serialization:
    - handles circular references
    - keeps legitimate alias/shared references
    - handles datetime/date
    - handles set/tuple/bytes
    :param obj: the object to process
    :param visited_ids: set of object ids on the current recursion path
    :return:
    """

    if visited_ids is None:
        visited_ids=set()  # set lookups are fast

    # fast path for JSON-native types
    if obj is None:
        return None

    if isinstance(obj, (bool, int, str)):
        return obj

    # Enum (e.g. RepresentationUnit.unit_type) is not a JSON-native type.
    # Export its stable value so exporters stay decoupled from domain
    # enum implementations.
    if isinstance(obj, Enum):
        return safe_serialize(obj.value, visited_ids)

    obj_id = id(obj) # dicts/lists are not hashable, so track memory addresses

    # circular reference
    if obj_id in visited_ids:
        return "<circular_reference>"

    # dataclass
    if is_dataclass(obj):
        return safe_serialize(  # recurse
            asdict(obj),  # converts the dataclass to a dict
            visited_ids
        )

    # dict
    if isinstance(obj,dict):
        visited_ids.add(obj_id)
        result = {
            _convert_key(k): safe_serialize(v,visited_ids)
            for k,v in obj.items()
        }
        visited_ids.remove(obj_id)
        return result

    # list
    elif isinstance(obj,list):
        visited_ids.add(obj_id)
        result =  [
            safe_serialize(v,visited_ids)
            for v in obj
        ]
        visited_ids.remove(obj_id)
        return result

    # tuple -> list (JSON has no tuple)
    elif isinstance(obj,tuple):
        visited_ids.add(obj_id)
        result = [
            safe_serialize(v,visited_ids)
            for v in obj
        ]
        visited_ids.remove(obj_id)
        return result

    # set -> list (JSON has no set)
    elif isinstance(obj,set):
        visited_ids.add(obj_id)
        result =  {
            "__type__": "set",
            "items": [
                safe_serialize(v, visited_ids)
                for v in obj
            ]
        }
        visited_ids.remove(obj_id)
        return result

    # datetime/date/time -> str (JSON has no datetime types)
    elif isinstance(obj,(datetime,date,time)):
        return obj.isoformat()

    # bytes
    elif isinstance(obj,bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "__type__": "bytes",
                "base64": base64.b64encode(obj).decode("ascii")
            }

    # float
    elif isinstance(obj,float):
        if math.isnan(obj):
            return "NaN"

        elif math.isinf(obj):
            if obj > 0:
                return "Infinity"
            else:
                return "-Infinity"

        return obj

    return obj
