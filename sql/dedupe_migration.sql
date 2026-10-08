-- Applied by `python -m fundo dedupe --apply` before its first merge. Idempotent.
-- In production this is a migration in the application's own repository.
IF COL_LENGTH('dbo.Customers', 'DedupeStatus') IS NULL
    ALTER TABLE dbo.Customers ADD
        DedupeStatus          VARCHAR(10) NULL,   -- NULL = active | merged | hold | test
        DuplicateOfCustomerId INT         NULL;   -- same person as this customer (merged into it, or held next to it)
-- A survivor can't be deleted while merged accounts point at it.
IF OBJECT_ID('dbo.FK_Customers_DuplicateOf') IS NULL
    ALTER TABLE dbo.Customers ADD CONSTRAINT FK_Customers_DuplicateOf
        FOREIGN KEY (DuplicateOfCustomerId) REFERENCES dbo.Customers (CustomerId);
IF SCHEMA_ID('ops') IS NULL EXEC('CREATE SCHEMA ops');
IF OBJECT_ID('ops.DedupeRun') IS NULL
    CREATE TABLE ops.DedupeRun (
        RunId        VARCHAR(40)   NOT NULL PRIMARY KEY,
        AppliedAt    DATETIME2(3)  NOT NULL,
        BackupFile   NVARCHAR(400) NULL,
        CreatedIndex BIT           NOT NULL,
        UndoneAt     DATETIME2(3)  NULL,
        Summary      NVARCHAR(400) NULL);
IF OBJECT_ID('ops.DedupeDecision') IS NULL
    CREATE TABLE ops.DedupeDecision (
        RunId            VARCHAR(40)   NOT NULL,
        CustomerId       INT           NOT NULL,
        Action           VARCHAR(20)   NOT NULL,  -- exclude_test | merge_into | hold
        TargetCustomerId INT           NULL,
        Reason           NVARCHAR(300) NOT NULL);
-- Every column value the dedupe changes, old and new: the way back.
IF OBJECT_ID('ops.DedupeChange') IS NULL
    CREATE TABLE ops.DedupeChange (
        ChangeId   BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        RunId      VARCHAR(40)   NOT NULL,
        TableName  VARCHAR(50)   NOT NULL,
        KeyColumn  VARCHAR(50)   NOT NULL,
        KeyValue   BIGINT        NOT NULL,
        ColumnName VARCHAR(50)   NOT NULL,
        OldValue   NVARCHAR(400) NULL,
        NewValue   NVARCHAR(400) NULL);
