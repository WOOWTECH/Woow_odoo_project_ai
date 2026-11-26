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


def stores_other_than(value, model_name):
    """Whether a create value of a model-name field (mail.message.model,
    ir.attachment.res_model...) is stored as something other than
    `model_name`: nothing (False, None) or a plain string that is not it. Any
    other type (bytes, an XML-RPC binary...) answers no: the caller checks the
    stored rows then."""
    return value is None or value is False or (isinstance(value, str) and value != model_name)


def defaults_set(records, field_names):
    """Whether a create of `records`' model may get one of `field_names` from a
    default (a default_* key of the context, or an ir.default): the caller
    then checks the stored rows."""
    context = records.env.context
    if any(context.get('default_' + name) for name in field_names):
        return True
    defaults = records.env['ir.default'].sudo()._get_model_defaults(records._name)
    return any(defaults.get(name) for name in field_names)
