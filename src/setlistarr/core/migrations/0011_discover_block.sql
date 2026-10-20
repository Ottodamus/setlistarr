-- Dismiss now only takes an artist off the waiting list (it can be suggested again after a while);
-- Block is the permanent "never suggest again". Dismissals made before this promised "never again",
-- so they become blocks.
UPDATE discoveries SET status = 'blocked' WHERE status = 'dismissed';
