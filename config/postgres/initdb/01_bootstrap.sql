-- ==========================================================================
-- InsightForge AI - PostgreSQL first-boot bootstrap (Phase 7 / spec Phase 14)
-- --------------------------------------------------------------------------
-- Executed once by the postgres:16 image, only when the data directory is
-- empty. The `insightforge` database and role are already created by the
-- image from POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD.
--
-- Kept deliberately minimal (development rule 10): the star schema and
-- operational tables are created in Phase 8; the connection layer in Phase 9.
-- ==========================================================================

-- Deterministic date/time handling across every session and container host.
ALTER DATABASE insightforge SET timezone TO 'UTC';

COMMENT ON DATABASE insightforge
    IS 'InsightForge AI - analytics store (provisioned Phase 7)';
