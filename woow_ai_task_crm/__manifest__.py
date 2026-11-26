# -*- coding: utf-8 -*-
{
    'name': 'Project AI - CRM',
    'version': '18.0.1.0.0',
    'category': 'Services/Project',
    'summary': 'AI form consultant for CRM leads (a template, off until a system administrator enables it)',
    'description': """
Project AI - CRM
================

「詢問 AI 顧問」 on CRM leads and opportunities, with the AI form consultant
of Project AI (woow_ai_task):

* "Ask the AI Consultant" continues the latest open consultation task of the
  lead the user may use; "New Topic" always starts a new one; the "AI
  Consultations" smart button lists the ones the user can read. The task is
  named after the lead and its internal AI chat opens in a chat window
* The internal AI gets the lead's type, stage, priority, company name,
  salesperson, sales team, expected revenue, probability, expected closing,
  tags, source, medium, campaign and lost reason, read with the asker's own
  rights. Never the contact fields (e-mail, phone, address, contact name),
  the chatter, the files or the activities. The lead's Notes are free text
  that can hold what visitors typed (a Live Chat conversation, a web form's
  extra fields): they are sent only while the setting's "Send the Notes" is
  on. E-mail addresses, phone numbers and IP addresses in the text sent are
  masked by patterns, which is not a guarantee; names and postal addresses
  are not masked. Nothing goes to the customer tab
* Once enabled, only people who can read the lead (Odoo's own lead rules)
  use its consultation chats; a reassigned, merged or deleted lead updates
  who may stay, and a deleted lead's consultation tasks lose its name
* Installed only on purpose, switched off. It installs CRM and CRM's own
  bridges for the apps already there (e.g. website_crm, crm_livechat).
  Where CRM is not installed yet: while Default Access Rights (Settings >
  Users) is on, which is Odoo's default, Odoo gives every existing employee
  the groups CRM adds to the default user template, Sales Administrator
  included, who reads every lead. Review the Sales rights before enabling
* Update Project AI (woow_ai_task 18.0.1.1.0) before installing it (it
  refuses otherwise, but by then Odoo has already installed CRM where it was
  missing), and keep it in every image that serves the database once
  installed

See the Project AI README and docs/PROJECT.md (「AI 顧問」).
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_project_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_task', 'crm'],
    'data': [],
    'pre_init_hook': 'pre_init_hook',
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'auto_install': False,
    'application': False,
}
