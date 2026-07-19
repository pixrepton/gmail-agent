-- P0 correlation registry metrics (safe read-only SQL)

-- Link coverage by type.
SELECT link_type, COUNT(*) AS n
FROM correlation_links
GROUP BY link_type
ORDER BY link_type;

-- Engagement coverage.
SELECT
  COUNT(DISTINCT engagement_id) AS engagements_total,
  COUNT(*) FILTER (WHERE link_type = 'mailbox_case') AS mailbox_case_links,
  COUNT(*) FILTER (WHERE link_type = 'cieplo_workflow') AS cieplo_workflow_links,
  COUNT(*) FILTER (WHERE link_type = 'gmail_message') AS gmail_message_links,
  COUNT(*) FILTER (WHERE link_type = 'identity_email') AS identity_email_links
FROM correlation_links;

-- Duplicate identity groups by normalized email (expected >0 on historical backfills; P1 cleanup).
SELECT COUNT(*) AS duplicate_email_identity_groups
FROM (
  SELECT lower(primary_email) AS email_norm, COUNT(DISTINCT identity_id) AS n
  FROM topinstal_identities
  WHERE primary_email <> ''
  GROUP BY lower(primary_email)
  HAVING COUNT(DISTINCT identity_id) > 1
) t;

-- Top duplicate groups (for inspection).
SELECT lower(primary_email) AS email_norm, COUNT(DISTINCT identity_id) AS n
FROM topinstal_identities
WHERE primary_email <> ''
GROUP BY lower(primary_email)
HAVING COUNT(DISTINCT identity_id) > 1
ORDER BY n DESC, email_norm ASC
LIMIT 50;

-- P2: duplicate engagements sharing the same gmail_message (merge candidates).
SELECT COUNT(*) AS duplicate_message_engagement_groups
FROM (
  SELECT target_id
  FROM correlation_links
  WHERE link_type = 'gmail_message' AND target_id <> ''
  GROUP BY target_id
  HAVING COUNT(DISTINCT engagement_id) > 1
) t;
