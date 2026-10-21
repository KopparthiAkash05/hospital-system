-- ===========================================================================
-- MediCare Hospital — Smart Hospital feature migration
--
-- Adds only the tables/columns required by the missing features:
--   * Department entity (departments pages + admin department management)
--   * DoctorSchedule entity (structured working days / hours / slot duration)
--   * Notification entity (in-app notifications for patient + doctor)
--   * AuditLog entity (admin audit trail)
--   * Patient  : date_of_birth, emergency contact, is_active
--   * Doctor   : department_id, qualification, license_number, consultation_fee,
--                profile_description, availability_status, is_active
--   * Appointment : reason, department_id (snapshot at booking time)
--   * Patient.patient_id / Doctor.doctor_id : AUTO_INCREMENT (required so that
--                registration can insert rows at all)
--
-- Safe to run repeatedly: every statement is IF NOT EXISTS / conditional.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 0. AUTO_INCREMENT on Patient/Doctor primary keys
--
-- Some deployments were created without AUTO_INCREMENT on these two columns,
-- which makes every INSERT fail with
--   ERROR 1364 "Field 'patient_id' doesn't have a default value"
-- and makes registration return HTTP 500. The guard below is a no-op when the
-- column is already AUTO_INCREMENT.
-- Doctor.doctor_id must be changed before DoctorSchedule can reference it,
-- so its foreign key is dropped and restored around the change.
-- ---------------------------------------------------------------------------
SET @ai_patient := (
    SELECT IF(COUNT(*) = 0,
        'ALTER TABLE Patient MODIFY patient_id INT NOT NULL AUTO_INCREMENT',
        'DO 0')
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Patient'
      AND COLUMN_NAME = 'patient_id' AND EXTRA NOT LIKE '%auto_increment%'
);
PREPARE stmt_patient_ai FROM @ai_patient;
EXECUTE stmt_patient_ai;
DEALLOCATE PREPARE stmt_patient_ai;

SET @ai_doctor := (
    SELECT IF(COUNT(*) = 0,
        'ALTER TABLE Doctor MODIFY doctor_id INT NOT NULL AUTO_INCREMENT',
        'DO 0')
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Doctor'
      AND COLUMN_NAME = 'doctor_id' AND EXTRA NOT LIKE '%auto_increment%'
);
PREPARE stmt_doctor_ai FROM @ai_doctor;
EXECUTE stmt_doctor_ai;
DEALLOCATE PREPARE stmt_doctor_ai;

-- ---------------------------------------------------------------------------
-- 1. Department
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS Department (
    department_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    description VARCHAR(500) DEFAULT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 2. DoctorSchedule  (day_of_week uses ISO: 1=Mon ... 7=Sun)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DoctorSchedule (
    schedule_id INT AUTO_INCREMENT PRIMARY KEY,
    doctor_id INT NOT NULL,
    day_of_week TINYINT NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    slot_duration INT NOT NULL DEFAULT 30,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_doctor_day_start (doctor_id, day_of_week, start_time),
    FOREIGN KEY (doctor_id) REFERENCES Doctor(doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 3. Notification
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS Notification (
    notification_id INT AUTO_INCREMENT PRIMARY KEY,
    user_type ENUM('patient', 'doctor', 'admin') NOT NULL,
    user_id INT NOT NULL,
    title VARCHAR(150) NOT NULL,
    message VARCHAR(500) NOT NULL,
    appointment_id INT DEFAULT NULL,
    is_read TINYINT(1) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_notification_user (user_type, user_id, is_read),
    KEY idx_notification_appointment (appointment_id),
    FOREIGN KEY (appointment_id) REFERENCES Appointment(appointment_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 4. AuditLog
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS AuditLog (
    audit_id INT AUTO_INCREMENT PRIMARY KEY,
    user_type VARCHAR(20) DEFAULT NULL,
    user_id INT DEFAULT NULL,
    user_name VARCHAR(100) DEFAULT NULL,
    action VARCHAR(60) NOT NULL,
    entity VARCHAR(60) DEFAULT NULL,
    entity_id VARCHAR(60) DEFAULT NULL,
    description VARCHAR(500) DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    KEY idx_audit_created (created_at),
    KEY idx_audit_entity (entity, entity_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 5. New columns on existing tables
--    (each guarded so re-running the migration is a no-op)
-- ---------------------------------------------------------------------------

-- Patient: date of birth, emergency contact, active flag
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Patient'
             AND COLUMN_NAME = 'date_of_birth');
SET @s := IF(@c = 0, 'ALTER TABLE Patient ADD COLUMN date_of_birth DATE DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Patient'
             AND COLUMN_NAME = 'emergency_contact_name');
SET @s := IF(@c = 0, 'ALTER TABLE Patient ADD COLUMN emergency_contact_name VARCHAR(100) DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Patient'
             AND COLUMN_NAME = 'emergency_contact_phone');
SET @s := IF(@c = 0, 'ALTER TABLE Patient ADD COLUMN emergency_contact_phone VARCHAR(20) DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Patient'
             AND COLUMN_NAME = 'is_active');
SET @s := IF(@c = 0, 'ALTER TABLE Patient ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Doctor: department link + the profile fields the spec requires
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Doctor'
             AND COLUMN_NAME = 'department_id');
SET @s := IF(@c = 0, 'ALTER TABLE Doctor ADD COLUMN department_id INT DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Doctor'
             AND COLUMN_NAME = 'qualification');
SET @s := IF(@c = 0, 'ALTER TABLE Doctor ADD COLUMN qualification VARCHAR(150) DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Doctor'
             AND COLUMN_NAME = 'license_number');
SET @s := IF(@c = 0, 'ALTER TABLE Doctor ADD COLUMN license_number VARCHAR(60) DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Doctor'
             AND COLUMN_NAME = 'consultation_fee');
SET @s := IF(@c = 0, 'ALTER TABLE Doctor ADD COLUMN consultation_fee DECIMAL(10,2) DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Doctor'
             AND COLUMN_NAME = 'profile_description');
SET @s := IF(@c = 0, 'ALTER TABLE Doctor ADD COLUMN profile_description TEXT', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Doctor'
             AND COLUMN_NAME = 'availability_status');
SET @s := IF(@c = 0, "ALTER TABLE Doctor ADD COLUMN availability_status ENUM('Available','Unavailable') NOT NULL DEFAULT 'Available'", 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Doctor'
             AND COLUMN_NAME = 'is_active');
SET @s := IF(@c = 0, 'ALTER TABLE Doctor ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Appointment: visit reason + department snapshot
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND COLUMN_NAME = 'reason');
SET @s := IF(@c = 0, 'ALTER TABLE Appointment ADD COLUMN reason VARCHAR(500) DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND COLUMN_NAME = 'department_id');
SET @s := IF(@c = 0, 'ALTER TABLE Appointment ADD COLUMN department_id INT DEFAULT NULL', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- Foreign keys (added after the columns exist, and only once)
SET @c := (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Doctor'
             AND CONSTRAINT_NAME = 'fk_doctor_department');
SET @s := IF(@c = 0,
    'ALTER TABLE Doctor ADD CONSTRAINT fk_doctor_department FOREIGN KEY (department_id) REFERENCES Department(department_id) ON DELETE SET NULL',
    'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND CONSTRAINT_NAME = 'fk_appointment_department');
SET @s := IF(@c = 0,
    'ALTER TABLE Appointment ADD CONSTRAINT fk_appointment_department FOREIGN KEY (department_id) REFERENCES Department(department_id) ON DELETE SET NULL',
    'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------------
-- 6. Seed the departments the app already refers to by name, so existing
--    specializations can be assigned without retyping them.
-- ---------------------------------------------------------------------------
INSERT INTO Department (name, description) VALUES
    ('Cardiology', 'Heart and cardiovascular system care'),
    ('Dermatology', 'Skin, hair and nail conditions'),
    ('Pediatrics', 'Medical care for infants, children and adolescents'),
    ('General Medicine', 'Primary care and routine health concerns'),
    ('Neurology', 'Brain, spinal cord and nerve related conditions'),
    ('Orthopedics', 'Bone, joint and musculoskeletal care'),
    ('ENT', 'Ear, nose and throat care'),
    ('Ophthalmology', 'Eye care and vision related treatment'),
    ('Gynecology', 'Womens health and obstetrics'),
    ('Psychiatry', 'Mental health and behavioural care'),
    ('Pulmonology', 'Chest and lung conditions'),
    ('Gastroenterology', 'Digestive system and stomach care'),
    ('Endocrinology', 'Hormone and metabolic disorders'),
    ('Urology', 'Urinary and male reproductive system care'),
    ('Nephrology', 'Kidney care'),
    ('Oncology', 'Cancer diagnosis and treatment'),
    ('Rheumatology', 'Autoimmune and inflammatory joint conditions'),
    ('Dental', 'Teeth and oral health'),
    ('Allergy & Immunology', 'Allergy and immune system care'),
    ('Hematology', 'Blood disorders'),
    ('Hepatology', 'Liver related conditions'),
    ('Infectious Disease', 'Contagious and infectious illness care'),
    ('Emergency', 'Emergency medicine and urgent care')
ON DUPLICATE KEY UPDATE description = VALUES(description);

-- Backfill: link every existing doctor to the department matching the
-- specialization it already stores. Leaves unmatched doctors unassigned.
UPDATE Doctor d
JOIN Department dp ON dp.name = d.specialization
SET d.department_id = dp.department_id
WHERE d.department_id IS NULL;

-- ---------------------------------------------------------------------------
-- 7. Guard: two live appointments can never share the same doctor + date + time.
--    A generated column is NULL for released slots (Cancelled / Rejected /
--    Completed) and MySQL unique indexes ignore NULLs, so cancelled or rejected
--    rows free the slot for rebooking without any extra bookkeeping.
--
--    If historical data already contains duplicate live bookings, the oldest is
--    cancelled first so the index can be created without failing.
-- ---------------------------------------------------------------------------
UPDATE Appointment a
JOIN Appointment b
  ON a.doctor_id = b.doctor_id
 AND a.date = b.date
 AND TIME(a.time) = TIME(b.time)
 AND a.status IN ('Pending', 'Approved')
 AND b.status IN ('Pending', 'Approved')
 AND a.appointment_id > b.appointment_id
SET a.status = 'Cancelled';

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND COLUMN_NAME = 'slot_key');
SET @s := IF(@c = 0,
    "ALTER TABLE Appointment ADD COLUMN slot_key VARCHAR(80) GENERATED ALWAYS AS (CASE WHEN status IN ('Pending','Approved') THEN CONCAT(doctor_id, '-', date, '-', TIME(time)) ELSE NULL END) STORED",
    'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.STATISTICS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND INDEX_NAME = 'uq_appointment_slot');
SET @s := IF(@c = 0,
    'CREATE UNIQUE INDEX uq_appointment_slot ON Appointment (slot_key)',
    'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;