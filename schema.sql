CREATE DATABASE IF NOT EXISTS hospital_db;
USE hospital_db;

-- Patient Table
CREATE TABLE IF NOT EXISTS Patient (
    patient_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    phone VARCHAR(20),
    age INT,
    gender ENUM('Male', 'Female', 'Other'),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Doctor Table
CREATE TABLE IF NOT EXISTS Doctor (
    doctor_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    specialization VARCHAR(100) NOT NULL,
    experience INT,
    email VARCHAR(100) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    phone VARCHAR(20),
    available_slots TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Admin Table
CREATE TABLE IF NOT EXISTS Admin (
    admin_id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(100) UNIQUE NOT NULL,
    password VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Appointment Table
CREATE TABLE IF NOT EXISTS Appointment (
    appointment_id INT AUTO_INCREMENT PRIMARY KEY,
    patient_id INT NOT NULL,
    doctor_id INT NOT NULL,
    date DATE NOT NULL,
    time TIME NOT NULL,
    status ENUM('Pending', 'Approved', 'Rejected', 'Completed', 'Cancelled') DEFAULT 'Pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (patient_id) REFERENCES Patient(patient_id) ON DELETE CASCADE,
    FOREIGN KEY (doctor_id) REFERENCES Doctor(doctor_id) ON DELETE CASCADE
);

-- Insert default admin (password: admin123)
INSERT INTO Admin (name, email, password) VALUES 
('Admin', 'admin@hospital.com', 'pbkdf2:sha256:260000$...');

-- Insert sample doctors
INSERT INTO Doctor (name, specialization, experience, email, password, phone, available_slots) VALUES
('Dr. John Smith', 'Cardiology', 15, 'doctor1@hospital.com', 'pbkdf2:sha256:260000$...', '1234567890', 'Mon-Fri: 9AM-5PM'),
('Dr. Sarah Johnson', 'Dermatology', 10, 'doctor2@hospital.com', 'pbkdf2:sha256:260000$...', '1234567891', 'Mon-Sat: 10AM-6PM'),
('Dr. Michael Brown', 'Pediatrics', 12, 'doctor3@hospital.com', 'pbkdf2:sha256:260000$...', '1234567892', 'Tue-Sat: 8AM-4PM');