import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sec_bulk', ROOT / 'scripts/download_2012_sec_bulk.py')
sec_bulk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sec_bulk)


def fact(**changes):
    row = dict(cik='1234', name='Example', form='10-K', filed='2012-02-01',
               accepted='2012-02-01 10:00:00', period='2011-12-31',
               ddate='2011-12-31', adsh='0000001234-12-000001',
               instance='example.xml', qtrs=4, uom='USD', value=2.0,
               tag='EarningsPerShareDiluted', version='us-gaap/2011',
               coreg=None, segments=None)
    row.update(changes)
    return row


class SecBulkTests(unittest.TestCase):
    def test_coherent_single_filing_and_no_future_data(self):
        rows = [fact(ddate='2010-12-31', value=1), fact(),
                fact(filed='2013-02-01', period='2012-12-31',
                     ddate='2012-12-31', value=9, adsh='0000001234-13-000001')]
        company = sec_bulk.extract_companies(rows)[0]
        self.assertEqual([r['eps'] for r in company['history']], [1, 2])
        self.assertEqual(company['accession'], '0000001234-12-000001')

    def test_ambiguous_or_dimensional_facts_never_silently_selected(self):
        rows = [fact(), fact(value=3), fact(value=9, segments='ClassOfStock=ClassA;')]
        company = sec_bulk.extract_companies(rows)[0]
        self.assertEqual(company['history'], [])
        self.assertEqual(company['conflicts'][0]['values'], [2, 3])

    def test_incomplete_amendment_does_not_replace_full_annual(self):
        rows = [fact(), fact(ddate='2010-12-31', value=1),
                fact(filed='2012-05-01', form='10-K/A', ddate='2010-12-31',
                     value=8, adsh='0000001234-12-000002')]
        company = sec_bulk.extract_companies(rows)[0]
        self.assertEqual(company['accession'], '0000001234-12-000001')

    def test_custom_tag_with_standard_name_and_coreg_are_rejected(self):
        self.assertEqual(sec_bulk.extract_companies([fact(version='0000001234-12-000001')]), [])
        self.assertEqual(sec_bulk.extract_companies([fact(coreg='OTHER')]), [])


if __name__ == '__main__':
    unittest.main()
