-- ===========================================================================
-- MediCare Hospital — Admin Dashboard completion migration
--
-- Adds only what the missing admin sections need:
--   * HospitalInfo entity (public hospital details managed by the admin)
--   * Admin.is_active     (so admins can enable/disable other admin accounts)
--   * Appointment reminder/booking columns used by the notification +
--     follow-up monitoring pages (guarded, in case an install predates them)
--
-- Every statement is guarded so re-running this file is a no-op.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 1. HospitalInfo  (intentionally a single logical row; the admin edits it)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS HospitalInfo (
    info_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(150) NOT NULL DEFAULT 'MediCare Hospital',
    tagline VARCHAR(200) DEFAULT NULL,
    address VARCHAR(300) DEFAULT NULL,
    phone VARCHAR(30) DEFAULT NULL,
    emergency_phone VARCHAR(30) DEFAULT NULL,
    email VARCHAR(120) DEFAULT NULL,
    opening_hours VARCHAR(200) DEFAULT NULL,
    about TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Seed a single default row so the admin page always has something to edit.
INSERT INTO HospitalInfo (name, tagline, address, phone, emergency_phone, email, opening_hours, about)
SELECT 'MediCare Hospital',
       'Quality healthcare with modern technology',
       '123 Health Street, Medical City',
       '+1 234 567 890',
       '+1 234 567 911',
       'info@smarthospital.com',
       'Mon - Sat: 8:00 AM - 8:00 PM',
       'MediCare Hospital provides quality healthcare services with modern technology, expert doctors and convenient online appointment booking.'
WHERE NOT EXISTS (SELECT 1 FROM HospitalInfo);

-- ---------------------------------------------------------------------------
-- 2. Admin.is_active — allow additional admin accounts to be deactivated.
--    Existing admins default to active.
-- ---------------------------------------------------------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Admin'
             AND COLUMN_NAME = 'is_active');
SET @s := IF(@c = 0, 'ALTER TABLE Admin ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------------
-- 3. Appointment follow-up / reminder columns used by the new admin views.
--    These are added defensively so a freshly created database (schema.sql +
--    migration_smart_hospital.sql) still has them.
-- ---------------------------------------------------------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND COLUMN_NAME = 'follow_up_date');
SET @s := IF(@c = 0, 'ALTER TABLE Appointment ADD COLUMN follow_up_date DATE DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND COLUMN_NAME = 'reminder_sent');
SET @s := IF(@c = 0, 'ALTER TABLE Appointment ADD COLUMN reminder_sent TINYINT(1) NOT NULL DEFAULT 0', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND COLUMN_NAME = 'follow_up_reminder_sent');
SET @s := IF(@c = 0, 'ALTER TABLE Appointment ADD COLUMN follow_up_reminder_sent TINYINT(1) NOT NULL DEFAULT 0', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Index to make the follow-up admin listing fast.
SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND INDEX_NAME = 'idx_appointment_follow_up');
SET @s := IF(@c = 0,
    'CREATE INDEX idx_appointment_follow_up ON Appointment (follow_up_date)',
    'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
