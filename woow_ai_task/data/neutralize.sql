-- Project AI (woow_ai_task): a neutralized copy never calls a real AI backend and takes no push from one.
-- odoo neutralize runs every installed module's file in one cursor and stops at the first error, so each
-- table is checked first (to_regclass): a database where the module is installed but its tables are not
-- there yet (an interrupted install, a copy taken mid-upgrade) can still be neutralized.
DO $$
BEGIN
    IF to_regclass('woow_project_ai_connection') IS NOT NULL THEN
        UPDATE woow_project_ai_connection
           SET endpoint_url = NULL,
               customer_safe = (backend_type = 'openai'),
               active = FALSE;
        IF EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema() AND table_name = 'woow_project_ai_connection'
                      AND column_name = 'api_key_secret') THEN
            UPDATE woow_project_ai_connection SET api_key_secret = NULL WHERE api_key_secret IS NOT NULL;
        END IF;
    END IF;
    IF to_regclass('woow_project_ai_assistant') IS NOT NULL THEN
        UPDATE woow_project_ai_assistant
           SET push_key_hash = NULL,
               push_key_hint = NULL
         WHERE push_key_hash IS NOT NULL OR push_key_hint IS NOT NULL;
    END IF;
END $$;
