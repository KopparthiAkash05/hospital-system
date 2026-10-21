-- phpMyAdmin SQL Dump
-- version 5.2.1
-- https://www.phpmyadmin.net/
--
-- Host: sql.freedb.tech
-- Generation Time: Oct 05, 2026 at 04:23 PM
-- Server version: 8.0.46-0ubuntu0.22.04.4
-- PHP Version: 8.4.1

SET SQL_MODE = "NO_AUTO_VALUE_ON_ZERO";
START TRANSACTION;
SET time_zone = "+00:00";


/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;

--
-- Database: `freedb_xor9rxN1`
--

-- --------------------------------------------------------

--
-- Table structure for table `Admin`
--

CREATE TABLE `Admin` (
  `admin_id` int NOT NULL,
  `name` varchar(100) NOT NULL,
  `email` varchar(100) NOT NULL,
  `password` varchar(255) NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- --------------------------------------------------------

--
-- Table structure for table `Appointment`
--

CREATE TABLE `Appointment` (
  `appointment_id` int NOT NULL,
  `patient_id` int NOT NULL,
  `doctor_id` int NOT NULL,
  `date` date NOT NULL,
  `time` time NOT NULL,
  `status` enum('Pending','Approved','Rejected','Completed','Cancelled','No-Show') DEFAULT 'Pending',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `reminder_sent` tinyint(1) DEFAULT '0',
  `follow_up_date` date DEFAULT NULL,
  `follow_up_reminder_sent` tinyint(1) DEFAULT '0'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

--
-- Dumping data for table `Appointment`
--

INSERT INTO `Appointment` (`appointment_id`, `patient_id`, `doctor_id`, `date`, `time`, `status`, `created_at`, `reminder_sent`, `follow_up_date`, `follow_up_reminder_sent`) VALUES
(1, 1, 1, '2026-06-30', '09:00:00', 'Completed', '2026-06-30 01:03:45', 0, NULL, 0),
(2, 2, 1, '2026-06-30', '09:30:00', 'Completed', '2026-06-30 01:07:32', 0, NULL, 0),
(3, 3, 1, '2026-07-02', '14:30:00', 'Completed', '2026-06-30 08:40:08', 0, NULL, 0),
(4, 4, 1, '2026-07-09', '11:30:00', 'Completed', '2026-06-30 09:11:59', 0, NULL, 0),
(5, 2, 1, '2026-07-01', '09:30:00', 'Completed', '2026-06-30 19:13:30', 0, NULL, 0),
(6, 2, 1, '2026-07-05', '11:30:00', 'Completed', '2026-07-02 17:24:33', 0, NULL, 0),
(7, 2, 2, '2026-07-03', '10:00:00', 'Completed', '2026-07-02 19:16:01', 0, NULL, 0),
(8, 5, 2, '2026-07-04', '10:00:00', 'Completed', '2026-07-03 18:19:28', 0, NULL, 0),
(9, 5, 1, '2026-07-04', '10:00:00', 'Completed', '2026-07-03 19:08:03', 0, NULL, 0),
(10, 5, 1, '2026-07-04', '10:00:00', 'Completed', '2026-07-03 19:50:41', 0, '2026-07-06', 1),
(11, 5, 2, '2026-07-07', '10:00:00', 'Completed', '2026-07-06 14:53:42', 0, NULL, 0),
(12, 2, 2, '2026-09-21', '10:00:00', 'Completed', '2026-09-20 09:45:06', 0, NULL, 0),
(13, 5, 2, '2026-09-21', '10:30:00', 'Completed', '2026-09-20 10:53:29', 0, NULL, 0),
(14, 5, 2, '2026-09-28', '10:00:00', 'Cancelled', '2026-09-27 05:17:19', 0, NULL, 0),
(15, 5, 1, '2026-10-03', '10:00:00', 'Completed', '2026-10-02 06:10:53', 0, NULL, 0),
(16, 2, 1, '2026-10-03', '10:30:00', 'Completed', '2026-10-02 12:46:05', 0, NULL, 0),
(17, 2, 1, '2026-10-03', '10:30:00', 'Completed', '2026-10-02 12:52:02', 1, NULL, 0),
(18, 5, 1, '2026-10-03', '11:00:00', 'Completed', '2026-10-02 12:54:36', 0, NULL, 0),
(19, 5, 2, '2026-10-03', '10:00:00', 'Cancelled', '2026-10-02 13:03:38', 0, NULL, 0),
(20, 5, 1, '2026-10-03', '10:00:00', 'Completed', '2026-10-02 13:15:16', 0, NULL, 0),
(21, 5, 1, '2026-10-03', '10:00:00', 'Completed', '2026-10-02 13:21:53', 1, NULL, 0),
(22, 5, 1, '2026-10-05', '10:00:00', 'Completed', '2026-10-04 16:47:33', 1, '2026-10-06', 1);

-- --------------------------------------------------------

--
-- Table structure for table `Doctor`
--

CREATE TABLE `Doctor` (
  `doctor_id` int NOT NULL,
  `name` varchar(100) NOT NULL,
  `specialization` varchar(100) NOT NULL,
  `experience` int DEFAULT NULL,
  `email` varchar(100) NOT NULL,
  `password` varchar(255) NOT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `city` varchar(100) DEFAULT NULL,
  `available_slots` text,
  `latitude` decimal(10,8) DEFAULT NULL,
  `longitude` decimal(11,8) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `reset_token` varchar(100) DEFAULT NULL,
  `reset_token_expiry` datetime DEFAULT NULL,
  `address` varchar(255) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

--
-- Dumping data for table `Doctor`
--

INSERT INTO `Doctor` (`doctor_id`, `name`, `specialization`, `experience`, `email`, `password`, `phone`, `city`, `available_slots`, `latitude`, `longitude`, `created_at`, `reset_token`, `reset_token_expiry`, `address`) VALUES
(1, 'Smith', 'Dermatology', 3, 'smith@gmail.com', 'scrypt:32768:8:1$yY8TVmaO7In64Gpr$2cd991c40aef99008f90727e277486564e1af36aa7eecd0ebcc0de68b1ea13f5ccf4bf4cb0b5f0d047858bd4353d18cbb34776febb27f676ec216653bf2ff26c', '1234567890', NULL, 'Mon-Fri: 9AM-5PM, \r\nSat: 10AM-4PM', NULL, NULL, '2026-06-29 18:21:17', NULL, NULL, NULL),
(2, 'David', 'Dental', 5, 'david@gmail.com', 'scrypt:32768:8:1$yY8TVmaO7In64Gpr$2cd991c40aef99008f90727e277486564e1af36aa7eecd0ebcc0de68b1ea13f5ccf4bf4cb0b5f0d047858bd4353d18cbb34776febb27f676ec216653bf2ff26c', '1234567890', NULL, 'Mon-Fri: 10AM-5PM', NULL, NULL, '2026-07-02 14:32:44', NULL, NULL, NULL);

-- --------------------------------------------------------

--
-- Table structure for table `Patient`
--

CREATE TABLE `Patient` (
  `patient_id` int NOT NULL,
  `name` varchar(100) NOT NULL,
  `email` varchar(100) NOT NULL,
  `password` varchar(255) NOT NULL,
  `phone` varchar(20) DEFAULT NULL,
  `age` int DEFAULT NULL,
  `gender` enum('Male','Female','Other') DEFAULT NULL,
  `address` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `reset_token` varchar(100) DEFAULT NULL,
  `reset_token_expiry` datetime DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

--
-- Dumping data for table `Patient`
--

INSERT INTO `Patient` (`patient_id`, `name`, `email`, `password`, `phone`, `age`, `gender`, `address`, `created_at`, `reset_token`, `reset_token_expiry`) VALUES
(1, 'john', 'john@gmail.com', 'pbkdf2:sha256:600000$mNnhlaCeUT8t4mnn$943b50771489bab69c821c9b83dff3e40274ac66c781f292f73526250dd13774', '1234567890', 22, 'Male', NULL, '2026-06-29 18:18:37', NULL, NULL),
(2, 'akash kopparthi', 'akashkopparthi@gmail.com', 'pbkdf2:sha256:600000$II1V5uaYYiEEhswJ$19a0ca7bfe8d50aa36604a648d79a3d4a7f6cf7ca389e33f190225757f01e082', '1234567890', 20, 'Male', '', '2026-06-29 18:28:59', 'qYuu1FhsG_jMjWZQqo3QDQ', '2026-10-03 15:20:23'),
(3, 'k.abhi', 'abhilashkotakonda2005@gmail.com', 'pbkdf2:sha256:600000$OeVfmsfCcGvGRN9B$86ee94b8d050e937f3b96b8875251b20a46d3e1cd70c333c30594518bea7064f', '7386652513', 20, 'Male', NULL, '2026-06-30 08:38:42', NULL, NULL),
(4, 'varalakshmi', 'varalakshmi@gist.edu.in', 'pbkdf2:sha256:600000$jvLLO9m99Et60ln8$8c70d42c47e13df39c99ac542d70795cde634abf692aa755e030361a04f5456b', '9966033125', 36, 'Female', NULL, '2026-06-30 09:10:28', NULL, NULL),
(5, 'KOPPARTHI VENKATA SAI AKASH', 'akashkopparthi7@gmail.com', 'pbkdf2:sha256:600000$zkCzidDePEjid2WI$3be8f37df27b286f5857224a9a1812466c49ad5a4953e2e14daca69243b4be6d', '9440994842', 21, 'Male', 'Balaji Nagar, Nellore', '2026-07-02 19:21:33', 'vMq8Kjl4_yWmyrhbnM2zBg', '2026-10-02 07:09:11');

--
-- Indexes for dumped tables
--

--
-- Indexes for table `Admin`
--
ALTER TABLE `Admin`
  ADD PRIMARY KEY (`admin_id`),
  ADD UNIQUE KEY `email` (`email`);

--
-- Indexes for table `Appointment`
--
ALTER TABLE `Appointment`
  ADD PRIMARY KEY (`appointment_id`),
  ADD KEY `patient_id` (`patient_id`),
  ADD KEY `doctor_id` (`doctor_id`);

--
-- Indexes for table `Doctor`
--
ALTER TABLE `Doctor`
  ADD PRIMARY KEY (`doctor_id`),
  ADD UNIQUE KEY `email` (`email`);

--
-- Indexes for table `Patient`
--
ALTER TABLE `Patient`
  ADD PRIMARY KEY (`patient_id`),
  ADD UNIQUE KEY `email` (`email`);

--
-- AUTO_INCREMENT for dumped tables
--

--
-- AUTO_INCREMENT for table `Admin`
--
ALTER TABLE `Admin`
  MODIFY `admin_id` int NOT NULL AUTO_INCREMENT;

--
-- AUTO_INCREMENT for table `Appointment`
--
ALTER TABLE `Appointment`
  MODIFY `appointment_id` int NOT NULL AUTO_INCREMENT, AUTO_INCREMENT=23;
COMMIT;

/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
