# Data Dictionary

Source: https://www.sec.gov/dera/data/financial-statement-data-sets

## numbers

| Column | Type | Nulls | Description | Example | Join |
|--------|------|-------|-------------|---------|------|
| adsh | VARCHAR | 0.0% | Accession number - Unique 20-character identifier for a filing | 0000002178-11-000032 | Appears in 3 tables, common join key |
| tag | VARCHAR | 0.0% | XBRL tag used for the financial concept | CashAndCashEquivalentsPeriodIncreaseDecrease | Appears in 3 tables, common join key |
| version | VARCHAR | 0.0% | Taxonomy Version | us-gaap/2011 | Appears in 3 tables, common join key |
| ddate | DATE | 0.0% | End date for the numeric value | 2010-06-30 |  |
| qtrs | BIGINT | 0.0% | Number of Quarters | 2 |  |
| uom | VARCHAR | 0.0% | Unit of Measure | USD |  |
| segments | VARCHAR | 53.2% | Segments | ClassOfStock=AdditionalPaidInCapital; |  |
| coreg | VARCHAR | 95.4% | Co-Registrant | AREC |  |
| value | DOUBLE | 3.8% | The numeric value of the financial fact | 21374000.0 |  |
| footnote | VARCHAR | 99.8% | Footnote | Under the 2014 restructuring plan, the Company recorded restructuring and oth... |  |

## presentations

| Column | Type | Nulls | Description | Example | Join |
|--------|------|-------|-------------|---------|------|
| adsh | VARCHAR | 0.0% | Accession number - Unique 20-character identifier for a filing | 0000002178-11-000032 | Appears in 3 tables, common join key |
| report | BIGINT | 0.0% | Report Number | 2 |  |
| line | BIGINT | 0.0% | Line Number | 2 |  |
| stmt | VARCHAR | 0.0% | Statement Type | IS |  |
| inpth | BIGINT | 0.0% | In Path | 0 |  |
| rfile | VARCHAR | 0.0% | Related File | H |  |
| tag | VARCHAR | 0.0% | XBRL tag used for the financial concept | RefiningAndMarketingRevenue | Appears in 3 tables, common join key |
| version | VARCHAR | 0.0% | Taxonomy Version | us-gaap/2011 | Appears in 3 tables, common join key |
| plabel | VARCHAR | 0.0% | Presentation Label | Marketing |  |
| negating | BIGINT | 0.0% | Negating Flag | 0 |  |

## submissions

| Column | Type | Nulls | Description | Example | Join |
|--------|------|-------|-------------|---------|------|
| adsh | VARCHAR | 0.0% | Accession number - Unique 20-character identifier for a filing | 0000002178-11-000032 | Appears in 3 tables, common join key |
| cik | VARCHAR | 0.0% | Central Index Key - 10-digit unique SEC identifier | 0000002178 |  |
| name | VARCHAR | 0.0% | Name of the registrant | ADAMS RESOURCES & ENERGY, INC. |  |
| sic | VARCHAR | 0.5% | Standard Industrial Classification code | 5172 |  |
| countryba | VARCHAR | 0.4% | Country of Business Address | US |  |
| stprba | VARCHAR | 9.1% | State of incorporation / Business Address State | TX |  |
| cityba | VARCHAR | 0.2% | City of Business Address | HOUSTON |  |
| zipba | VARCHAR | 0.4% | Zip Code of Business Address | 77027 |  |
| bas1 | VARCHAR | 0.2% | Business Address Street 1 | 4400 POST OAK PKWY STE 2700 |  |
| bas2 | VARCHAR | 56.2% | Business Address Street 2 | P O BOX 844 |  |
| baph | VARCHAR | 0.3% | Business Address Phone | 7138813600 |  |
| countryma | VARCHAR | 1.2% | Country of Mailing Address | US |  |
| stprma | VARCHAR | 9.5% | State/Province of Mailing Address | TX |  |
| cityma | VARCHAR | 0.9% | City of Mailing Address | HOUSTON |  |
| zipma | VARCHAR | 1.1% | Zip Code of Mailing Address | 77001 |  |
| mas1 | VARCHAR | 1.0% | Mailing Address Street 1 | P O BOX 844 |  |
| mas2 | VARCHAR | 56.8% | Mailing Address Street 2 | MS 68 |  |
| countryinc | VARCHAR | 9.0% | Country of Incorporation | US |  |
| stprinc | VARCHAR | 14.7% | State/Province of Incorporation | DE |  |
| ein | VARCHAR | 1.1% | Employer Identification Number | 741753147 |  |
| former | VARCHAR | 46.2% | Former Name | ADAMS RESOURCES & ENERGY INC |  |
| changed | BIGINT | 46.2% | Date of Name Change | 19920703 |  |
| afs | VARCHAR | 0.4% | Accelerated Filer Status | 4-NON |  |
| wksi | BIGINT | 0.0% | Well-Known Seasoned Issuer | 1 |  |
| fye | VARCHAR | 0.1% | Fiscal Year End | 1231 |  |
| form | VARCHAR | 0.0% | Form type (e.g., 10-K, 10-Q) | 10-Q |  |
| period | DATE | 0.0% | End date of the reporting period | 2011-06-30 |  |
| fy | BIGINT | 2.7% | Fiscal Year | 2011 |  |
| fp | VARCHAR | 2.7% | Fiscal Period | Q2 |  |
| filed | DATE | 0.0% | Date the filing was submitted to the SEC | 2011-08-12 |  |
| accepted | TIMESTAMP | 0.0% | Timestamp the filing was accepted | 2011-08-12 13:57:00 |  |
| prevrpt | BIGINT | 0.0% | Previous Report | 0 |  |
| detail | BIGINT | 0.0% | Detail | 0 |  |
| instance | VARCHAR | 0.0% | Instance Document | ae-20110630.xml |  |
| nciks | BIGINT | 0.0% | Number of CIKs | 1 |  |
| aciks | VARCHAR | 98.2% | Additional CIKs | 22198 50172 73986 81027 92487 |  |

## tags

| Column | Type | Nulls | Description | Example | Join |
|--------|------|-------|-------------|---------|------|
| tag | VARCHAR | 0.0% | XBRL tag used for the financial concept | A | Appears in 3 tables, common join key |
| version | VARCHAR | 0.0% | Taxonomy Version | 0001185185-21-001713 | Appears in 3 tables, common join key |
| custom | BIGINT | 0.0% | Custom Tag | 1 |  |
| abstract | BIGINT | 0.0% | Abstract Tag | 0 |  |
| datatype | VARCHAR | 0.0% | Data Type | monetary |  |
| iord | VARCHAR | 0.0% | Instant or Duration | D |  |
| crdr | VARCHAR | 11.6% | Credit or Debit | C |  |
| tlabel | VARCHAR | 0.0% | Human-readable label for the tag | A |  |
| doc | VARCHAR | 9.3% | Detailed SEC definition for the financial concept | Additional Paid in Capital - Warrants |  |
