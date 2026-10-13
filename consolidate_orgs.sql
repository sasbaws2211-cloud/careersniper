-- ============================================================================
-- PHASE 1: Data Consolidation - Merge all orgs into org_id=1 (Westfield Career Center)
-- Run this BEFORE schema changes (Phase 2)
-- ============================================================================

-- Primary org to keep (has growth plan, most data)
-- org_id=1: 'Westfield Career Center', slug='westfield', plan='growth'

-- ============================================================================
-- 1. USERS: Move org 2 & 3 users to org 1
--    Note: Email is unique, so we need to handle conflicts
-- ============================================================================
UPDATE users SET org_id = 1 WHERE org_id IN (2, 3);

-- ============================================================================
-- 2. COMPANIES: Move org 2 & 3 owned companies to org 1
--    Shared registry companies (owner_org_id=NULL) stay as-is
-- ============================================================================
UPDATE companies SET owner_org_id = 1 WHERE owner_org_id IN (2, 3);

-- ============================================================================
-- 3. SUBSCRIBERS: Move to org 1
--    Email+org_id unique constraint - check for conflicts first
-- ============================================================================
-- Check for duplicate emails across orgs
-- SELECT email, COUNT(*) FROM subscribers GROUP BY email HAVING COUNT(*) > 1;
UPDATE subscribers SET org_id = 1 WHERE org_id IN (2, 3);

-- ============================================================================
-- 4. MEMBERS: Move to org 1
--    Unique constraint on (org_id, email) - check conflicts
-- ============================================================================
UPDATE members SET org_id = 1 WHERE org_id IN (2, 3);

-- ============================================================================
-- 5. INVITES: Move to org 1
-- ============================================================================
UPDATE invites SET org_id = 1 WHERE org_id IN (2, 3);

-- ============================================================================
-- 6. CLICK_EVENTS: Move to org 1
-- ============================================================================
UPDATE click_events SET org_id = 1 WHERE org_id IN (2, 3);

-- ============================================================================
-- 7. EMPLOYER_USERS: Move to org 1
--    Unique constraint on (org_id, email) - check conflicts
-- ============================================================================
UPDATE employer_users SET org_id = 1 WHERE org_id IN (2, 3);

-- ============================================================================
-- 8. AI_USAGE: Move to org 1
-- ============================================================================
UPDATE ai_usage SET org_id = 1 WHERE org_id IN (2, 3);

-- ============================================================================
-- VERIFY: Check all org_id values are now 1 or NULL
-- ============================================================================
-- SELECT 'users' AS table_name, org_id, COUNT(*) FROM users GROUP BY org_id
-- UNION ALL SELECT 'companies', owner_org_id, COUNT(*) FROM companies GROUP BY owner_org_id
-- UNION ALL SELECT 'subscribers', org_id, COUNT(*) FROM subscribers GROUP BY org_id
-- UNION ALL SELECT 'members', org_id, COUNT(*) FROM members GROUP BY org_id
-- UNION ALL SELECT 'invites', org_id, COUNT(*) FROM invites GROUP BY org_id
-- UNION ALL SELECT 'click_events', org_id, COUNT(*) FROM click_events GROUP BY org_id
-- UNION ALL SELECT 'employer_users', org_id, COUNT(*) FROM employer_users GROUP BY org_id
-- UNION ALL SELECT 'ai_usage', org_id, COUNT(*) FROM ai_usage GROUP BY org_id;

-- ============================================================================
-- AFTER VERIFICATION: Delete orgs 2 and 3 (keep org 1)
-- ============================================================================
-- DELETE FROM orgs WHERE id IN (2, 3);

-- ============================================================================
-- NOTE: The following tables have NO org_id column (shared/global):
--   jobs, companies (shared registry), billing_checkouts, billing_events,
--   billing_provider_plans, subscriptions, applications, saved_jobs,
--   resumes, documents, interview_sessions, company_logos
-- ============================================================================