# -*- coding: utf-8 -*-
"""Every model: Odoo reads a message with sudo wherever it does not call
``read`` (``mail.message.fetch`` is sudo), and a related or computed field
reads a linked file with sudo too: an onchange computes on a new record from
the values sent (im_livechat's ``parent_body`` reads the parent message's
body, the full composer its subject, partners and record name, a letter its
file), and a record linking a message or a file shows those values to whoever
reads it. So a message or a file of a task AI conversation is used there only
by someone who can read that conversation:

* an onchange that references one (in the values sent, nested lines and
  records included, in the ``default_*`` context, or the record itself);
* a record created or written with a link to one (many2one, many2many,
  one2many commands; ``default_*`` context on create).

Superuser (Odoo's own flows run with sudo) and every other record: unchanged."""

from odoo import api, models

GUARDED_COMODELS = ('mail.message', 'ir.attachment')
MAX_DEPTH = 6


def _is_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _collect_value(model, field, value, refs, depth):
    """Add to `refs` the guarded record ids in `value` (a many2one: id,
    (id, name), {'id': ..., other values}; an x2many: ids or commands)."""
    if not value or depth > MAX_DEPTH:
        return
    comodel = model.env[field.comodel_name]
    guarded = refs.get(field.comodel_name)
    if field.type == 'many2one':
        ident = value
        if isinstance(value, dict):
            ident = value.get('id')
            _collect(comodel, value, refs, depth + 1)
        elif isinstance(value, (list, tuple)):
            ident = value[0] if value else None
        if guarded is not None and _is_id(ident):
            guarded.add(ident)
        return
    if not isinstance(value, (list, tuple)):
        return
    for item in value:
        ids, vals = (), None
        if _is_id(item):
            ids = (item,)
        elif isinstance(item, (list, tuple)) and item:
            if item[0] in (0, 1) and len(item) > 2:
                vals = item[2]
            if item[0] in (1, 2, 3, 4) and len(item) > 1:
                ids = (item[1],)
            elif item[0] == 6 and len(item) > 2 and isinstance(item[2], (list, tuple)):
                ids = item[2]
        if guarded is not None:
            guarded.update(i for i in ids if _is_id(i))
        if isinstance(vals, dict):
            _collect(comodel, vals, refs, depth + 1)


def _collect(model, values, refs, depth=0):
    if depth > MAX_DEPTH or not isinstance(values, dict):
        return
    for name, value in values.items():
        field = model._fields.get(name)
        if field is not None and field.relational and value:
            _collect_value(model, field, value, refs, depth)


class Base(models.AbstractModel):
    _inherit = 'base'

    def onchange(self, values, field_names, fields_spec):
        if not self.env.su:
            refs = self._woow_project_ai_new_refs()
            if self._name in refs:
                refs[self._name].update(i for i in self.ids if _is_id(i))
            _collect(self, values, refs)
            self._woow_project_ai_collect_defaults(refs)
            self._woow_project_ai_check_refs(refs)
        return super().onchange(values, field_names, fields_spec)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            refs = self._woow_project_ai_new_refs()
            for vals in vals_list:
                _collect(self, vals, refs)
            self._woow_project_ai_collect_defaults(refs)
            self._woow_project_ai_check_refs(refs)
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            refs = self._woow_project_ai_new_refs()
            _collect(self, vals, refs)
            self._woow_project_ai_check_refs(refs)
        return super().write(vals)

    @api.model
    def _woow_project_ai_new_refs(self):
        return {name: set() for name in GUARDED_COMODELS}

    def _woow_project_ai_collect_defaults(self, refs):
        for key, value in self.env.context.items():
            if isinstance(key, str) and key.startswith('default_'):
                field = self._fields.get(key[8:])
                if field is not None and field.relational:
                    _collect_value(self, field, value, refs, 0)

    def _woow_project_ai_check_refs(self, refs):
        """Refuse the messages and files of task AI chats among `refs` whose
        chat the current user cannot read."""
        if refs['mail.message']:
            self.env['mail.message']._woow_project_ai_check_readable(list(refs['mail.message']))
        if refs['ir.attachment']:
            chats = self.env['ir.attachment']._woow_project_ai_file_chats(list(refs['ir.attachment']))
            if chats:
                self.env['discuss.channel'].browse(sorted(set(chats.values()))).check_access('read')
