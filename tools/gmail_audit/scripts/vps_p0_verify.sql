SELECT link_type, COUNT(*) AS n FROM correlation_links GROUP BY link_type ORDER BY 1;
SELECT COUNT(DISTINCT engagement_id) AS engagements,
       COUNT(*) FILTER (WHERE link_type = 'mailbox_case') AS mailbox_cases,
       COUNT(*) FILTER (WHERE link_type = 'identity_email') AS identity_emails
FROM correlation_links;
SELECT indexname FROM pg_indexes
WHERE tablename = 'topinstal_identities' AND indexdef ILIKE '%UNIQUE%primary_email%';
