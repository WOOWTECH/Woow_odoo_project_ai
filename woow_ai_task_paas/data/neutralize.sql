-- a copy for testing never brings the old platform task AI back: the switch goes away
DELETE FROM ir_config_parameter WHERE key = 'woow_ai_task_paas.legacy_task_ai';
