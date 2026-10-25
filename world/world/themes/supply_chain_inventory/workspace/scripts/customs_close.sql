-- Close customs document-freeze windows whose cutoff has passed.
-- Runs against the world warehouse attached as `wh` (world.executor `sql` kind),
-- after the nightly dbt build has refreshed warehouse.customs_cutoff. Once a
-- declaration's freeze end is at or before the latest loaded business day the
-- window is closed: the documents can no longer be amended.
update warehouse.customs_cutoff
set window_status = 'FROZEN',
    declaration_status = 'CLOSED'
where documents_frozen
  and window_status <> 'FROZEN';
