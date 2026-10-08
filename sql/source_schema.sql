-- Source database, shaped like the operational SQL Server we replicate from.
-- Run by `python -m fundo seed` against an empty database named `fundo`.

CREATE TABLE dbo.Customers (
    CustomerId    INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Customers PRIMARY KEY,
    FirstName     NVARCHAR(100) NOT NULL,
    LastName      NVARCHAR(100) NOT NULL,
    Email         NVARCHAR(320) NULL,          -- free text from the signup form
    Phone         NVARCHAR(50)  NULL,          -- free text, any format
    DateOfBirth   DATE          NULL,          -- collected at KYC
    Ssn           CHAR(9)       NULL,          -- digits only, collected at KYC; NULL before KYC
    AddressLine1  NVARCHAR(200) NULL,
    City          NVARCHAR(100) NULL,
    State         CHAR(2)       NULL,
    PostalCode    VARCHAR(10)   NULL,
    CreatedAt     DATETIME2(3)  NOT NULL,
    UpdatedAt     DATETIME2(3)  NOT NULL
);

CREATE TABLE dbo.Advances (
    AdvanceId    INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Advances PRIMARY KEY,
    CustomerId   INT           NOT NULL CONSTRAINT FK_Advances_Customers REFERENCES dbo.Customers (CustomerId),
    Amount       DECIMAL(10,2) NOT NULL,
    Fee          DECIMAL(10,2) NOT NULL,
    Status       VARCHAR(20)   NOT NULL,       -- requested | approved | declined | cancelled | funded | paid_off
    RequestedAt  DATETIME2(3)  NOT NULL,
    FundedAt     DATETIME2(3)  NULL,
    PaidOffAt    DATETIME2(3)  NULL,
    UpdatedAt    DATETIME2(3)  NOT NULL
);
CREATE INDEX IX_Advances_CustomerId ON dbo.Advances (CustomerId);

-- Bad schema choice, kept on purpose: the owning customer is stored as unbounded
-- text. NVARCHAR(MAX) cannot be indexed or carry a foreign key, and legacy imports
-- left values like ' 1234' and '001234' that only match after normalisation.
CREATE TABLE dbo.Cards (
    CardId       INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Cards PRIMARY KEY,
    CustomerId   NVARCHAR(MAX) NOT NULL,
    Brand        VARCHAR(20)   NOT NULL,
    Last4        CHAR(4)       NOT NULL,
    Fingerprint  CHAR(16)      NOT NULL,       -- processor fingerprint of the card number
    ExpMonth     TINYINT       NOT NULL,
    ExpYear      SMALLINT      NOT NULL,
    IsDefault    BIT           NOT NULL,
    Status       VARCHAR(20)   NOT NULL,       -- active | expired | removed
    CreatedAt    DATETIME2(3)  NOT NULL,
    UpdatedAt    DATETIME2(3)  NOT NULL
);

-- The large table: mostly historical, rarely changed after insert.
CREATE TABLE dbo.Transactions (
    TransactionId BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_Transactions PRIMARY KEY,
    AdvanceId     INT           NOT NULL CONSTRAINT FK_Transactions_Advances REFERENCES dbo.Advances (AdvanceId),
    CardId        INT           NULL     CONSTRAINT FK_Transactions_Cards REFERENCES dbo.Cards (CardId),
    Type          VARCHAR(20)   NOT NULL,      -- disbursement | repayment | fee | refund
    Amount        DECIMAL(10,2) NOT NULL,
    Status        VARCHAR(20)   NOT NULL,      -- pending | settled | failed
    CreatedAt     DATETIME2(3)  NOT NULL,
    SettledAt     DATETIME2(3)  NULL
);
CREATE INDEX IX_Transactions_AdvanceId ON dbo.Transactions (AdvanceId);

-- Append-only history, kept forever today. No FK: history should outlive the row.
CREATE TABLE dbo.AdvanceStatusHistory (
    HistoryId   BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_AdvanceStatusHistory PRIMARY KEY,
    AdvanceId   INT           NOT NULL,
    FromStatus  VARCHAR(20)   NULL,
    ToStatus    VARCHAR(20)   NOT NULL,
    ChangedAt   DATETIME2(3)  NOT NULL,
    ChangedBy   NVARCHAR(100) NOT NULL,
    RowVer      ROWVERSION    NOT NULL         -- drives the append-only watermark
);
CREATE INDEX IX_AdvanceStatusHistory_RowVer ON dbo.AdvanceStatusHistory (RowVer);

-- Scratch table nobody owns: a one-off export someone made in 2021. No key.
CREATE TABLE dbo.tmp_CustomerExport_2021 (
    Id          INT           NULL,
    Email       NVARCHAR(320) NULL,
    Phone       NVARCHAR(50)  NULL,
    ExportedAt  DATETIME      NULL,
    Notes       NVARCHAR(MAX) NULL
);
