-- Additive migration for owner-scoped collection requests. No existing data is
-- modified. Run before deploying the Baltic search/request API.
BEGIN;
SET LOCAL lock_timeout = '3s';
CREATE TABLE IF NOT EXISTS public.collection_requests (
    id SERIAL PRIMARY KEY,
    owner_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    filters TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    CONSTRAINT uq_collection_request_owner_scope UNIQUE (owner_id, request_key)
);
CREATE INDEX IF NOT EXISTS idx_collection_request_active
    ON public.collection_requests (expires_at, owner_id);
ALTER TABLE public.collection_requests ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.collection_requests FROM PUBLIC, anon, authenticated;
REVOKE ALL ON SEQUENCE public.collection_requests_id_seq FROM PUBLIC, anon, authenticated;
COMMIT;
SELECT c.relname, c.relrowsecurity,
       has_table_privilege('anon', c.oid, 'SELECT') AS anon_select,
       has_table_privilege('authenticated', c.oid, 'SELECT') AS authenticated_select
FROM pg_class c WHERE c.oid = 'public.collection_requests'::regclass;
