      ******************************************************************
      * CLAIMS.CPY - NIGHTLY CLAIMS EXTRACT RECORD LAYOUT              *
      * SOURCE: POLICYMASTER CLAIMS (SYSTEM OF RECORD)                 *
      * RECORD: FIXED BLOCK, 31 BYTES, NO RECORD DELIMITERS            *
      * CHARACTER FIELDS ARE CODE PAGE 037 (EBCDIC)                    *
      * CLAIM-AMT IS PACKED DECIMAL: 9 DIGITS + SIGN = 5 BYTES,        *
      * IMPLIED 2 DECIMAL PLACES (I.E. STORED AS CENTS)                *
      ******************************************************************
       01  CLAIMS-EXTRACT-RECORD.
           05  CLAIM-ID                PIC X(10).
           05  CLAIM-DATE              PIC 9(8).      *> YYYYMMDD
           05  CUST-ID                 PIC X(8).
           05  CLAIM-AMT               PIC S9(7)V99 COMP-3.
