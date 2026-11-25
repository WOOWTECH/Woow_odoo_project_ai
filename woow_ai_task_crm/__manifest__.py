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
  tags, source, medium, campaign, lost reason and notes, read with the
  asker's own rights. Never the contact details, the address, the chatter,
  the files or the activities. Nothing goes to the customer tab
* Once enabled, only people who can read the lead (Odoo's own lead rules)
  use its consultation chats; a reassigned, merged or deleted lead updates
  who may stay
* Installed only on purpose (it installs CRM and CRM's own bridges for the
  apps already there, e.g. website_crm, crm_livechat), switched off; keep it
  in every image that serves the database once installed

See the Project AI README and docs/PROJECT.md (「AI 顧問」).
    """,
    'author': 'WOOWTECH',
    'website': 'https://github.com/WOOWTECH/Woow_odoo_project_ai',
    'license': 'LGPL-3',
    'depends': ['woow_ai_task', 'crm'],
    'data': [
        'data/consult_profile.xml',
    ],
    'installable': True,
    'auto_install': False,
    'application': False,
}
