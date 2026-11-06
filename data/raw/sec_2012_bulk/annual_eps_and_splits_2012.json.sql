
        SELECT n.adsh, n.tag, n.version, n.ddate, n.qtrs, n.uom,
               n.segments, n.coreg, n.value, s.cik, s.name, s.form, s.period, s.filed, s.accepted,
               s.fy, s.fp, s.instance
        FROM sec.numbers n JOIN vintage s USING (adsh)
        WHERE n.ddate <= DATE '2012-12-31' AND ((n.qtrs = 4 AND n.tag IN ('EarningsPerShareDiluted', 'EarningsPerShareBasicAndDiluted'))
           OR n.tag ILIKE '%StockSplit%'
           OR n.tag ILIKE '%StockSplits%')
    