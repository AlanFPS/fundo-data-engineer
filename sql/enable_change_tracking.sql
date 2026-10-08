-- Enabled after the seed so the bulk load does not fill the change tables.
-- Snapshot isolation lets the pipeline read CHANGE_TRACKING_CURRENT_VERSION() and
-- every table's changes from one consistent point in time.
ALTER DATABASE fundo SET ALLOW_SNAPSHOT_ISOLATION ON;
ALTER DATABASE fundo SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 7 DAYS, AUTO_CLEANUP = ON);
ALTER TABLE dbo.Customers    ENABLE CHANGE_TRACKING;
ALTER TABLE dbo.Advances     ENABLE CHANGE_TRACKING;
ALTER TABLE dbo.Cards        ENABLE CHANGE_TRACKING;
ALTER TABLE dbo.Transactions ENABLE CHANGE_TRACKING;
-- AdvanceStatusHistory is append-only and uses its ROWVERSION watermark instead.
