SELECT count(*) AS workflows_total FROM workflows;
SELECT count(*) AS cieplo_links FROM correlation_links WHERE link_type = 'cieplo_workflow';
