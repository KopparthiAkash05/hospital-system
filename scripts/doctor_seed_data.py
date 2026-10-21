"""Seed data for the MediCare Hospital doctor directory.

This module is the single source of truth for the seeded doctors. Both the
Python seeder (``scripts/seed_doctors.py``) and the generated SQL file
(``database/seed_doctors.sql``) are built from the data defined here, so the
two can never drift apart.

Conventions used by the seed
----------------------------
* Credentials: every seeded doctor gets the email ``<name>@gmail.com`` and the
  password ``doctor123<name>`` (all lower case, spaces removed). The email
  prefix lives in ``EMAIL_PREFIX`` so it can be changed in one place.
* Phone numbers: the ``89`` / ``63`` / ``78`` mobile blocks are used for demo
  data only, with a zero padded tail that cannot be dialled by accident. These
  numbers are placeholders for the UI and are not meant for real calls or SMS.
* Profile: the city column stays empty for the seeded doctors; the address and
  the map coordinates carry the location instead.
* Schedule: every doctor gets at least one structured ``DoctorSchedule`` row,
  which is what the automatic availability engine reads. The free text
  ``available_slots`` column mirrors the same hours as a fallback.
"""

EMAIL_PREFIX = ''
EMAIL_DOMAIN = 'gmail.com'
PASSWORD_PREFIX = 'doctor123'
PHONE_PREFIXES = ('89', '63', '78')

# Department descriptions, used when a department row has to be created.
DEPARTMENT_DESCRIPTIONS = {
    'Cardiology': 'Heart and cardiovascular system care',
    'Dermatology': 'Skin, hair and nail conditions',
    'Pediatrics': 'Medical care for infants, children and adolescents',
    'General Medicine': 'Primary care and routine health concerns',
    'Neurology': 'Brain, spinal cord and nerve related conditions',
    'Orthopedics': 'Bone, joint and musculoskeletal care',
    'ENT': 'Ear, nose and throat care',
    'Ophthalmology': 'Eye care and vision related treatment',
    'Gynecology': 'Womens health and obstetrics',
    'Psychiatry': 'Mental health and behavioural care',
    'Pulmonology': 'Chest and lung conditions',
    'Gastroenterology': 'Digestive system and stomach care',
    'Endocrinology': 'Hormone and metabolic disorders',
    'Urology': 'Urinary and male reproductive system care',
    'Nephrology': 'Kidney care',
    'Oncology': 'Cancer diagnosis and treatment',
    'Rheumatology': 'Autoimmune and inflammatory joint conditions',
    'Dental': 'Teeth and oral health',
    'Allergy & Immunology': 'Allergy and immune system care',
    'Hematology': 'Blood disorders',
    'Hepatology': 'Liver related conditions',
    'Infectious Disease': 'Contagious and infectious illness care',
    'Emergency Medicine': 'Emergency medicine and urgent care',
}

# Weekly working patterns. ``day_of_week`` uses the ISO numbering (1 = Monday,
# 7 = Sunday). Slot duration is 30 minutes everywhere.
SCHEDULE_PATTERNS = {
    'P1': {
        'description': 'Monday to Saturday, 9:00 AM - 5:00 PM',
        'free_text': 'Mon-Sat: 9:00 AM - 5:00 PM',
        'windows': [(day, '09:00', '17:00') for day in range(1, 7)],
    },
    'P2': {
        'description': 'Monday to Saturday, 10:00 AM - 4:30 PM',
        'free_text': 'Mon-Sat: 10:00 AM - 4:30 PM',
        'windows': [(day, '10:00', '16:30') for day in range(1, 7)],
    },
    'P3': {
        'description': 'Monday to Friday, 9:00 AM - 5:00 PM',
        'free_text': 'Mon-Fri: 9:00 AM - 5:00 PM',
        'windows': [(day, '09:00', '17:00') for day in range(1, 6)],
    },
    'P4': {
        'description': 'Monday to Saturday, 10:00 AM - 4:00 PM',
        'free_text': 'Mon-Sat: 10:00 AM - 4:00 PM',
        'windows': [(day, '10:00', '16:00') for day in range(1, 7)],
    },
}

SLOT_DURATION = 30

# Real city coordinates so the "nearby doctors" page can sort by distance.
CITIES = {
    'Ahmedabad': (23.0225, 72.5714),
    'Bengaluru': (12.9716, 77.5946),
    'Bhopal': (23.2599, 77.4126),
    'Bhubaneswar': (20.2961, 85.8245),
    'Chandigarh': (30.7333, 76.7794),
    'Chennai': (13.0827, 80.2707),
    'Coimbatore': (11.0168, 76.9558),
    'Hyderabad': (17.3850, 78.4867),
    'Indore': (22.7196, 75.8577),
    'Jaipur': (26.9124, 75.7873),
    'Kochi': (9.9312, 76.2673),
    'Kolkata': (22.5726, 88.3639),
    'Lucknow': (26.8467, 80.9462),
    'Madurai': (9.9252, 78.1198),
    'Mumbai': (19.0760, 72.8777),
    'Nagpur': (21.1458, 79.0882),
    'New Delhi': (28.6139, 77.2090),
    'Patna': (25.5941, 85.1376),
    'Pune': (18.5204, 73.8567),
    'Vijayawada': (16.5062, 80.6480),
    'Visakhapatnam': (17.6868, 83.2185),
}

# name, department, pattern, location, experience, license number,
# profile description
DOCTORS = [
    # ---------------------------------------------------------------- Cardio
    ('Ananya Sharma', 'Cardiology', 'P1', 'Chennai',
     10, 'MHMC-2008-0101',
     'Interventional cardiologist with 10 years of experience in coronary care and '
     'heart rhythm management. Runs a dedicated hypertension clinic.'),
    ('Vikram Iyer', 'Cardiology', 'P3', 'Bengaluru',
     9, 'MHMC-2013-0102',
     'Cardiologist focused on echocardiography and preventive cardiology for '
     'young adults with family history of heart disease.'),
    ('Priya Nair', 'Cardiology', 'P4', 'Kochi',
     6, 'MHMC-2017-0103',
     'Paediatric and congenital cardiology fellow known for a calm, family '
     'friendly approach and clear counselling.'),

    # ------------------------------------------------------------- Neurology
    ('Rohan Mehta', 'Neurology', 'P1', 'Mumbai',
     9, 'MHMC-2007-0104',
     'Consultant neurologist treating stroke, epilepsy and movement disorders. '
     'Leads the hospital stroke pathway.'),
    ('Kavya Reddy', 'Neurology', 'P2', 'Hyderabad',
     8, 'MHMC-2015-0105',
     'Neurologist with a special interest in migraine, dizziness and sleep '
     'disorders. Runs a video follow-up clinic for chronic cases.'),
    ('Arjun Desai', 'Neurology', 'P4', 'Pune',
     5, 'MHMC-2018-0106',
     'Neurologist focused on epilepsy and clinical neurophysiology, with '
     'experience in video EEG monitoring.'),

    # ----------------------------------------------------------- Orthopedics
    ('Sanjay Kapoor', 'Orthopedics', 'P1', 'New Delhi',
     10, 'MHMC-2004-0107',
     'Senior orthopedic surgeon specialising in joint replacement and complex '
     'revision hip surgery.'),
    ('Meera Joshi', 'Orthopedics', 'P3', 'Pune',
     8, 'MHMC-2012-0108',
     'Orthopedic surgeon with a sports injury practice, treating ligament tears '
     'and repetitive strain problems.'),
    ('Rahul Verma', 'Orthopedics', 'P4', 'Lucknow',
     6, 'MHMC-2016-0109',
     'Orthopedic surgeon working on paediatric fractures and corrective surgery '
     'for growing children.'),

    # ------------------------------------------------------ General Medicine
    ('Sneha Kulkarni', 'General Medicine', 'P1', 'Bengaluru',
     8, 'MHMC-2009-0110',
     'General physician handling everyday health needs, chronic disease '
     'reviews and annual check-ups for working adults.'),
    ('Amit Bhatt', 'General Medicine', 'P2', 'Ahmedabad',
     9, 'MHMC-2014-0111',
     'General physician with an interest in diabetes management and lifestyle '
     'related health coaching.'),
    ('Divya Menon', 'General Medicine', 'P4', 'Kochi',
     5, 'MHMC-2017-0112',
     'General physician offering same day consultations for fever, infections '
     'and routine follow-up visits.'),

    # ------------------------------------------------------------ Pediatrics
    ('Nikhil Rao', 'Pediatrics', 'P1', 'Hyderabad',
     9, 'MHMC-2011-0113',
     'Paediatrician handling newborn care, immunisation schedules and growth '
     'follow-up for infants.'),
    ('Pooja Chandra', 'Pediatrics', 'P3', 'Chennai',
     8, 'MHMC-2015-0114',
     'Paediatrician specialising in childhood asthma, allergies and nutrition '
     'advice for parents.'),
    ('Sameer Pandey', 'Pediatrics', 'P4', 'Lucknow',
     5, 'MHMC-2018-0115',
     'Paediatrician with a friendly approach to developmental screening and '
     'paediatric emergency care.'),

    # ------------------------------------------------------------ Gynecology
    ('Anjali Mishra', 'Gynecology', 'P1', 'New Delhi',
     10, 'MHMC-2006-0116',
     'Obstetrician and gynaecologist with 10 years of experience in high risk '
     'pregnancy care and laparoscopic surgery.'),
    ('Ritu Saxena', 'Gynecology', 'P2', 'Jaipur',
     8, 'MHMC-2014-0117',
     'Gynaecologist providing fertility counselling, menstrual disorder care '
     'and routine antenatal checks.'),
    ('Shruti Agarwal', 'Gynecology', 'P4', 'Indore',
     5, 'MHMC-2018-0118',
     'Gynaecologist offering adolescent health services, contraception '
     'counselling and screening for cervical cancer.'),

    # ----------------------------------------------------------- Dermatology
    ('Tanvi Bhardwaj', 'Dermatology', 'P1', 'New Delhi',
     9, 'MHMC-2012-0119',
     'Dermatologist treating chronic eczema, psoriasis and acne with a focus '
     'on long term skin care plans.'),
    ('Varun Grover', 'Dermatology', 'P3', 'Jaipur',
     7, 'MHMC-2015-0120',
     'Dermatologist covering hair fall, cosmetic dermatology and procedural '
     'treatment of skin conditions.'),
    ('Nisha Fernandes', 'Dermatology', 'P4', 'Mumbai',
     4, 'MHMC-2018-0121',
     'Dermatologist with a paediatric and infectious skin disease practice, '
     'including patch testing for allergies.'),

    # -------------------------------------------------------- Ophthalmology
    ('Aditya Malhotra', 'Ophthalmology', 'P1', 'Chandigarh',
     9, 'MHMC-2011-0122',
     'Ophthalmologist performing cataract surgery and managing diabetic eye '
     'disease with regular screening clinics.'),
    ('Swara Ranganathan', 'Ophthalmology', 'P4', 'Chennai',
     6, 'MHMC-2017-0123',
     'Ophthalmologist focused on retina, glaucoma and paediatric vision '
     'screening.'),

    # ------------------------------------------------------------------ ENT
    ('Ramesh Shetty', 'ENT', 'P2', 'Bengaluru',
     9, 'MHMC-2013-0124',
     'ENT surgeon dealing with sinus disease, ear disorders and routine tonsil '
     'and adenoid procedures.'),
    ('Isha Khanna', 'ENT', 'P4', 'New Delhi',
     5, 'MHMC-2017-0125',
     'ENT specialist treating hearing loss, vertigo and allergies with voice '
     'and speech therapy referrals.'),

    # ----------------------------------------------------- Emergency Medicine
    ('Sandeep Ghosh', 'Emergency Medicine', 'P1', 'Kolkata',
     9, 'MHMC-2014-0126',
     'Emergency physician leading triage, resuscitation and stabilisation of '
     'critical cases on arrival.'),
    ('Preeti Suri', 'Emergency Medicine', 'P3', 'Ahmedabad',
     6, 'MHMC-2016-0127',
     'Emergency physician with experience in trauma care, snake bites and '
     'medical emergencies.'),

    # ---------------------------------------------------------------- Dental
    ('Karthik Varma', 'Dental', 'P1', 'Kochi',
     9, 'MHMC-2011-0128',
     'Dentist providing root canal care, crowns and preventive dentistry with '
     'gentle treatment plans.'),
    ('Swathi Nambiar', 'Dental', 'P3', 'Coimbatore',
     7, 'MHMC-2015-0129',
     'Dentist focused on gum disease treatment, dental implants and oral '
     'hygiene coaching.'),

    # ------------------------------------------------------------- Psychiatry
    ('Manish Tiwari', 'Psychiatry', 'P2', 'Bhopal',
     9, 'MHMC-2010-0130',
     'Psychiatrist managing depression, anxiety and bipolar disorder with '
     'supportive medication review sessions.'),

    # ----------------------------------------------------------- Pulmonology
    ('Gaurav Bansal', 'Pulmonology', 'P1', 'New Delhi',
     10, 'MHMC-2009-0131',
     'Pulmonologist treating asthma, COPD and interstitial lung disease with '
     'spirometry based follow-up.'),

    # ------------------------------------------------------- Gastroenterology
    ('Rajeev Thakur', 'Gastroenterology', 'P4', 'Mumbai',
     8, 'MHMC-2014-0132',
     'Gastroenterologist performing endoscopy and managing acid reflux, '
     'ulcer care and liver related complaints.'),

    # ------------------------------------------------------- Endocrinology
    ('Ankita Chauhan', 'Endocrinology', 'P3', 'Chandigarh',
     7, 'MHMC-2015-0133',
     'Endocrinologist treating diabetes and thyroid disorders, with a strong '
     'focus on lifestyle counselling.'),

    # --------------------------------------------------------------- Urology
    ('Deepak Jain', 'Urology', 'P2', 'Jaipur',
     9, 'MHMC-2013-0134',
     'Urologist managing kidney stones, urinary obstruction and prostate '
     'conditions with minimally invasive procedures.'),

    # ----------------------------------------------------------- Nephrology
    ('Sunil Yadav', 'Nephrology', 'P1', 'Lucknow',
     8, 'MHMC-2012-0135',
     'Nephrologist managing chronic kidney disease, dialysis planning and '
     'post transplant follow-up care.'),
    ('Pallavi Rathore', 'Nephrology', 'P4', 'Indore',
     5, 'MHMC-2018-0136',
     'Nephrologist treating glomerular disease, hypertension related kidney '
     'problems and electrolyte disorders.'),

    # -------------------------------------------------------------- Oncology
    ('Manoj Rana', 'Oncology', 'P3', 'Kolkata',
     10, 'MHMC-2006-0137',
     'Medical oncologist with 10 years of experience in breast and lung cancer '
     'chemotherapy planning.'),
    ('Anuradha Sen', 'Oncology', 'P4', 'Chennai',
     9, 'MHMC-2014-0138',
     'Radiation oncologist planning radiotherapy for cancer patients with a '
     'multidisciplinary tumour board approach.'),

    # ---------------------------------------------------------- Rheumatology
    ('Paresh Vyas', 'Rheumatology', 'P2', 'Bhopal',
     8, 'MHMC-2014-0139',
     'Rheumatologist treating rheumatoid arthritis, lupus and other '
     'autoimmune conditions with joint care plans.'),

    # ------------------------------------------------- Allergy & Immunology
    ('Alka Sinha', 'Allergy & Immunology', 'P4', 'Patna',
     7, 'MHMC-2015-0140',
     'Immunologist assessing allergies, autoimmune conditions and recurrent '
     'infections with allergy testing.'),

    # ---------------------------------------------------------- Hematology
    ('Ravi Prasad', 'Hematology', 'P1', 'Vijayawada',
     9, 'MHMC-2012-0141',
     'Haematologist managing anaemia, clotting disorders and blood cancer '
     'with coordinated transfusion support.'),

    # ---------------------------------------------------------- Hepatology
    ('Nita Gaur', 'Hepatology', 'P3', 'New Delhi',
     9, 'MHMC-2013-0142',
     'Hepatologist treating viral hepatitis, cirrhosis and fatty liver disease '
     'with nutrition and lifestyle guidance.'),

    # --------------------------------------------------- Infectious Disease
    ('Ajay Dutta', 'Infectious Disease', 'P4', 'Kolkata',
     8, 'MHMC-2014-0143',
     'Infectious disease physician treating tuberculosis, HIV and tropical '
     'fever with hospital infection control support.'),
]

# Working windows for the two doctors that already exist. Their free text
# availability is kept exactly as it was; the structured rows simply mirror it
# so the automatic availability engine works for them too.
EXISTING_DOCTOR_SCHEDULES = {
    'smith@gmail.com': [(day, '09:00', '17:00') for day in range(1, 6)] + [(6, '10:00', '16:00')],
    'david@gmail.com': [(day, '10:00', '17:00') for day in range(1, 6)],
}


def name_slug(name):
    """Lower case doctor name without spaces, used for email and password."""
    return ''.join(character for character in name.lower() if character.isalnum())


def email_for(name):
    """Demo email address for a doctor."""
    return f'{EMAIL_PREFIX}{name_slug(name)}@{EMAIL_DOMAIN}'


def password_for(name):
    """Demo password for a doctor."""
    return f'{PASSWORD_PREFIX}{name_slug(name)}'


def phone_for(index):
    """Demo phone number for the n-th seeded doctor (1 based).

    Rotates through the 89 / 63 / 78 mobile blocks with a zero padded tail so
    the number is clearly placeholder data and not a real subscriber.
    """
    prefix = PHONE_PREFIXES[(index - 1) % len(PHONE_PREFIXES)]
    return f'{prefix}000000{index:02d}'


def doctor_records():
    """Every seeded doctor as a dict, ready to be written to the database."""
    records = []
    for index, (name, department, pattern, location, experience,
                license_number, description) in enumerate(DOCTORS, start=1):
        latitude, longitude = CITIES[location]
        records.append({
            'name': name,
            'specialization': department,
            'department': department,
            'experience': experience,
            'email': email_for(name),
            'password': password_for(name),
            'phone': phone_for(index),
            # The city column stays empty for the seeded doctors; the map
            # coordinates are what the nearby search actually uses.
            'city': None,
            'location': location,
            'address': None,
            'latitude': latitude,
            'longitude': longitude,
            'available_slots': SCHEDULE_PATTERNS[pattern]['free_text'],
            'qualification': None,
            'license_number': license_number,
            # No consultation fee is seeded; doctors set their own if they want.
            'consultation_fee': None,
            'profile_description': description,
            'availability_status': 'Available',
            'is_active': 1,
            'pattern': pattern,
            'schedule': SCHEDULE_PATTERNS[pattern]['windows'],
        })
    return records
