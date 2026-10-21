-- ===========================================================================
-- MediCare Hospital — Patient feedback & prescription management migration
--
-- Adds only the tables/columns required by the new features:
--   * Feedback            (one review per completed appointment, with rating,
--                          optional comment and an admin moderation flag)
--   * Prescription        (one prescription per completed appointment)
--   * PrescriptionMedicine(line items: medicine, dosage, frequency, duration)
--   * Appointment.feedback_email_sent (tracks the Brevo feedback invitation
--                          so a retried completion never emails the patient twice)
--
-- Every statement is guarded so re-running this file is a no-op.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 0. Legacy Feedback table
--
-- An earlier, unfinished attempt created a token-based Feedback table that
-- cannot support editing or moderation (NOT NULL token columns, no
-- is_hidden/updated_at). That table is replaced here, but ONLY while it
-- contains zero reviews, so no submitted feedback can ever be lost. If a
-- deployment already holds reviews in the legacy shape, the drop is skipped.
-- ---------------------------------------------------------------------------
SET @has_feedback := (SELECT COUNT(*) FROM information_schema.TABLES
                      WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Feedback');
SET @legacy_feedback := (SELECT COUNT(*) FROM information_schema.COLUMNS
                         WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Feedback'
                           AND COLUMN_NAME = 'token');
SET @feedback_rows := IF(@has_feedback = 1, (SELECT COUNT(*) FROM Feedback), 0);
SET @drop_legacy := IF(@has_feedback = 1 AND @legacy_feedback = 1 AND @feedback_rows = 0,
                       'DROP TABLE Feedback', 'DO 0');
PREPARE stmt_drop FROM @drop_legacy; EXECUTE stmt_drop; DEALLOCATE PREPARE stmt_drop;

-- ---------------------------------------------------------------------------
-- 1. Feedback
--    One row per appointment (UNIQUE), so duplicate submissions are rejected
--    at the database level as well as in the application.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS Feedback (
    feedback_id INT AUTO_INCREMENT PRIMARY KEY,
    appointment_id INT NOT NULL,
    patient_id INT NOT NULL,
    doctor_id INT NOT NULL,
    rating TINYINT NOT NULL,
    comment TEXT DEFAULT NULL,
    is_hidden TINYINT(1) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_feedback_appointment (appointment_id),
    KEY idx_feedback_patient (patient_id),
    KEY idx_feedback_doctor (doctor_id),
    KEY idx_feedback_rating (rating),
    CONSTRAINT chk_feedback_rating CHECK (rating BETWEEN 1 AND 5),
    FOREIGN KEY (appointment_id) REFERENCES Appointment(appointment_id) ON DELETE CASCADE,
    FOREIGN KEY (patient_id) REFERENCES Patient(patient_id) ON DELETE CASCADE,
    FOREIGN KEY (doctor_id) REFERENCES Doctor(doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Compatibility guards: bring an existing compatible Feedback table up to the
-- full column set used by the application (moderation flag + edit timestamp).
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Feedback'
             AND COLUMN_NAME = 'is_hidden');
SET @s := IF(@c = 0, 'ALTER TABLE Feedback ADD COLUMN is_hidden TINYINT(1) NOT NULL DEFAULT 0', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Feedback'
             AND COLUMN_NAME = 'updated_at');
SET @s := IF(@c = 0, 'ALTER TABLE Feedback ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---------------------------------------------------------------------------
-- 2. Prescription
--    One prescription per appointment (UNIQUE) so repeated form submissions
--    can never create duplicates.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS Prescription (
    prescription_id INT AUTO_INCREMENT PRIMARY KEY,
    appointment_id INT NOT NULL,
    patient_id INT NOT NULL,
    doctor_id INT NOT NULL,
    instructions TEXT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_prescription_appointment (appointment_id),
    KEY idx_prescription_patient (patient_id),
    KEY idx_prescription_doctor (doctor_id),
    FOREIGN KEY (appointment_id) REFERENCES Appointment(appointment_id) ON DELETE CASCADE,
    FOREIGN KEY (patient_id) REFERENCES Patient(patient_id) ON DELETE CASCADE,
    FOREIGN KEY (doctor_id) REFERENCES Doctor(doctor_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 3. PrescriptionMedicine — one row per prescribed medicine.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS PrescriptionMedicine (
    medicine_id INT AUTO_INCREMENT PRIMARY KEY,
    prescription_id INT NOT NULL,
    medicine_name VARCHAR(150) NOT NULL,
    dosage VARCHAR(100) NOT NULL,
    frequency VARCHAR(100) NOT NULL,
    duration VARCHAR(100) NOT NULL,
    KEY idx_prescription_medicine (prescription_id),
    FOREIGN KEY (prescription_id) REFERENCES Prescription(prescription_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------------
-- 4. Appointment.feedback_email_sent — mirrors the existing reminder_sent
--    pattern so the feedback invitation is sent at most once per appointment.
-- ---------------------------------------------------------------------------
SET @c := (SELECT COUNT(*) FROM information_schema.COLUMNS
           WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'Appointment'
             AND COLUMN_NAME = 'feedback_email_sent');
SET @s := IF(@c = 0, 'ALTER TABLE Appointment ADD COLUMN feedback_email_sent TINYINT(1) NOT NULL DEFAULT 0', 'DO 0');
PREPARE stmt FROM @s; EXECUTE stmt; DEALLOCATE PREPARE stmt;
