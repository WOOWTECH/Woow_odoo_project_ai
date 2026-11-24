# -*- coding: utf-8 -*-
"""Record ids as Odoo receives them over JSON-RPC: call_kw browses whatever
the client sent (4242, 4242.0, '4242'), and SQL accepts all three while the
cache and isinstance(id, int) do not. The module's own checks work on the
stored rows; these helpers turn the ids of a recordset into plain ints (or
refuse them) before such a check."""

from odoo.exceptions import AccessError


def as_id(value):
    """A plain int for an int, an integral float or a string of digits; None
    for anything else (bool, NewId, other strings and floats)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        value = value.strip()
        return int(value) if value.isdigit() and int(value) > 0 else None
    return None


def real_ids(records):
    """The ids of the real records of `records` as plain ints (new records
    left out); AccessError for any other id."""
    ids = []
    for id_ in records._ids:
        if not id_:  # NewId
            continue
        value = as_id(id_)
        if value is None:
            raise AccessError(records.env._('Invalid record id: %(id)r', id=id_))
        ids.append(value)
    return ids


def require_int_ids(records):
    """Outside superuser, refuse a recordset whose real ids are not plain ints
    (a float or a string id that Odoo would store or match in SQL)."""
    if records.env.su:
        return
    for id_ in records._ids:
        if id_ and not (isinstance(id_, int) and not isinstance(id_, bool)):
            raise AccessError(records.env._('Invalid record id: %(id)r', id=id_))
