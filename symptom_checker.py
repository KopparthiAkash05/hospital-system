"""
Rule-based symptom -> department suggestion.

This is triage assistance, NOT a diagnosis. Anything urgent should go to Emergency.

Improvements over the original substring lookup:
  * whole-word / whole-phrase matching with simple inflection (cough, coughing, coughs)
  * longest match wins, so "heart attack" does not also fire the generic "heart"
  * large synonym vocabulary (including common Indian-English wording)
  * negation handling ("I don't have a fever but my chest pain is severe")
  * first-listed department of each symptom is weighted highest
  * emergency red flags and pediatric context are scored explicitly
  * typo tolerance for longer words
  * ranked top-N output (rank_departments) in addition to the single best guess
"""
import re
from difflib import get_close_matches

GM, ID, PULM, ENT, NEURO = "General Medicine", "Infectious Disease", "Pulmonology", "ENT", "Neurology"
CARD, EMER, GASTRO, ORTHO = "Cardiology", "Emergency", "Gastroenterology", "Orthopedics"
DERM, OPH, DENT, RHEUM = "Dermatology", "Ophthalmology", "Dental", "Rheumatology"
ENDO, PSY, PSYCH, GYN, OBS = "Endocrinology", "Psychiatry", "Psychology", "Gynecology", "Obstetrics"
PED, ALLERGY, URO, NEPH = "Pediatrics", "Allergy & Immunology", "Urology", "Nephrology"
HEM, ONC, HEP = "Hematology", "Oncology", "Hepatology"

# (comma separated phrases, [departments in priority order], weak?)
# "weak" entries are body-part words that only hint at a department.
_ENTRIES = [
    # --- fever / infection ---
    ("fever,high temperature,temperature,chills,flu,viral", [GM, ID], False),
    ("body ache,body pain,body pains", [GM], False),
    ("malaria,dengue,typhoid,covid,infection", [ID, GM], False),
    ("tuberculosis,tb", [PULM, ID], False),
    # --- respiratory ---
    ("cough,cold", [GM, PULM], False),
    ("cold", [GM, ENT], False),
    ("coughing up blood,cough up blood,cough with blood,blood in sputum", [PULM, EMER], False),
    ("asthma,wheeze,wheezing", [PULM, ALLERGY], False),
    ("shortness of breath,short of breath,breathless,breathlessness,breathing problem,"
     "difficulty breathing,trouble breathing,breathing difficulty", [PULM, CARD], False),
    ("cant breathe,cannot breathe,unable to breathe,gasping", [EMER, PULM], False),
    ("chest infection,chest congestion,phlegm,sputum", [PULM, GM], False),
    ("chest", [CARD, PULM], True),
    # --- ENT / allergy ---
    ("runny nose,stuffy nose,blocked nose,nasal congestion,congestion,sneezing", [ENT, ALLERGY], False),
    ("allergy,allergic,hay fever,seasonal allergy", [ALLERGY, ENT], False),
    ("hives", [ALLERGY, DERM], False),
    ("sore throat,throat pain,throat ache,throat infection,tonsil,tonsillitis,"
     "pain swallowing,pain when swallowing,painful swallowing,difficulty swallowing,swallowing,throat", [ENT, GM], False),
    ("sinus,sinusitis,sinus infection,facial pressure,nosebleed,nose bleed,snoring,nose", [ENT], False),
    ("ear pain,earache,ear ache,ear infection,ringing in ear,tinnitus,hearing loss,cant hear,"
     "hard of hearing,ear discharge,blocked ear,deaf,deafness,ear", [ENT], False),
    # --- cardiology ---
    ("chest pain,chest tightness,tight chest,chest discomfort,chest pressure", [CARD, EMER], False),
    ("heart attack", [CARD, EMER], False),
    ("heart,cardiac,palpitation,heart racing,racing heart,irregular heartbeat,heartbeat,"
     "high blood pressure,low blood pressure,hypertension,blood pressure,bp,cholesterol", [CARD], False),
    # --- neuro ---
    ("headache,head ache,head pain,pounding head,head hurts,head", [GM, NEURO], False),
    ("migraine", [NEURO], False),
    ("seizure,fits,epilepsy,epileptic,convulsion", [NEURO], False),
    ("numbness,numb,tingling,pins and needles,paralysis,tremor,shaking,nerve,brain", [NEURO], False),
    ("memory loss,forgetful,forgetting,forgot,forgetfulness,dementia,confusion,confused,alzheimer,parkinson", [NEURO], False),
    ("insomnia,cant sleep,cannot sleep,trouble sleeping,sleeplessness,sleep problem,sleep problems,"
     "sleeping problem,sleep disorder", [NEURO, PSY], False),
    ("dizzy,dizziness,lightheaded,light headed,giddiness,giddy,spells", [NEURO, ENT], False),
    ("vertigo,room spinning,spinning", [ENT, NEURO], False),
    ("stroke,paralysed,paralyzed", [EMER, NEURO], False),
    # --- gastro ---
    ("stomach pain,stomach ache,stomachache,belly pain,tummy ache,tummy pain,stomach cramps", [GASTRO, GM], False),
    ("stomach,belly,tummy,abdomen,gut", [GASTRO, GM], True),
    ("abdominal pain,abdominal", [GASTRO, EMER], False),
    ("nausea,nauseous,vomit,vomiting,throwing up,throw up,puke,puking", [GASTRO, GM], False),
    ("diarrhea,diarrhoea,loose motion,loose stool,loose stools,watery stool", [GASTRO], False),
    ("constipation,constipated", [GASTRO], False),
    ("acid reflux,reflux,heartburn,acidity,burning chest,indigestion,bloating,gas problem,ulcer", [GASTRO], False),
    ("piles,hemorrhoids,haemorrhoids,blood in stool", [GASTRO], False),
    ("jaundice,yellow eyes,yellow skin,hepatitis,liver,fatty liver,gallbladder,gall stone", [GASTRO, HEP], False),
    # --- orthopedics / rheumatology ---
    ("back pain,backache,lower back,upper back,spine,slipped disc,sciatica,neck pain", [ORTHO, NEURO], False),
    ("joint pain,joint,knee pain,muscle pain,muscle pains,muscle ache", [ORTHO, RHEUM], False),
    ("arthritis,rheumatoid,swollen joint,swollen joints,morning stiffness,stiffness,gout,lupus", [RHEUM, ORTHO], False),
    ("fracture,fractured,broken bone,broken arm,broken leg,broken wrist,broken ankle,bone fracture,"
     "sprain,sprained,dislocated,dislocation,bone pain,bone", [ORTHO], False),
    ("knee,shoulder,hip,ankle,wrist,elbow,leg,arm,neck,foot,heel", [ORTHO], True),
    # --- skin ---
    ("rash,acne,pimple,eczema,psoriasis,dandruff,wart,fungal,ringworm,scabies,boil,blister,patches", [DERM], False),
    ("itch,itching,itchy", [DERM], True),
    ("hair loss,hair fall,hair falling out,hair falling,losing hair,balding,bald", [DERM, ENDO], False),
    ("mole,skin lesion", [DERM, ONC], False),
    ("skin cancer", [ONC, DERM], False),
    ("skin", [DERM], True),
    # --- eyes ---
    ("eye pain,eye infection,blurry vision,blurred vision,vision loss,loss of vision,cant see,vision,"
     "cataract,glaucoma,conjunctivitis,pink eye,stye,watery eyes,red eye", [OPH], False),
    ("eye,eyes", [OPH], True),
    # --- dental ---
    ("toothache,tooth pain,tooth,teeth,gum,cavity,root canal,dentist,wisdom tooth,gum pain,jaw pain", [DENT], False),
    # --- endocrine ---
    ("diabetes,diabetic,blood sugar,sugar level,sugar,high blood sugar,thyroid,goiter,hormone,hormonal", [ENDO], False),
    ("weight loss,losing weight,weight gain,gaining weight,unexplained weight loss", [ENDO, GM], False),
    ("fatigue,fatigued,tired,tiredness,exhausted,exhaustion,weakness,lethargy,lethargic,low energy", [GM, ENDO], True),
    # --- mental health ---
    ("anxiety,anxious,panic,panic attack,depression,depressed,hopeless,sadness,low mood,stress,stressed,"
     "mental health,worry,bipolar,ocd,phobia,mood swings,trauma,ptsd,addiction", [PSY, PSYCH], False),
    ("counselling,counseling,therapy,therapist", [PSYCH, PSY], False),
    ("suicidal,want to die,self harm,kill myself", [PSY, EMER], False),
    # --- women's health ---
    ("pregnant,pregnancy,prenatal,antenatal,postnatal,delivery,labour,labor,miscarriage,trimester,morning sickness", [GYN, OBS], False),
    ("period,period pain,menstrual,menstruation,menstrual cramps,pcos,pcod,menopause,vaginal,"
     "white discharge,irregular periods,missed period,heavy periods,painful periods", [GYN], False),
    ("breast", [GYN, ONC], True),
    # --- urology / nephrology ---
    ("urinary,urine,urination,urinating,urinate,peeing,pee,burning urination,painful urination,"
     "blood in urine,frequent urination,uti,urinary tract infection,urine infection,bladder", [URO, NEPH], False),
    ("kidney,kidneys,kidney disease,chronic kidney disease,dialysis,creatinine", [NEPH, URO], False),
    ("kidney stone,kidney stones,stone in kidney", [URO, NEPH], False),
    ("prostate,erectile,testicle,testicular", [URO], False),
    # --- blood ---
    ("anemia,anaemia,anemic,anaemic,low hemoglobin,low haemoglobin,hemoglobin,blood clot,clotting,pale,pallor,bruising,bruise", [HEM, GM], False),
    ("bleeding", [HEM, EMER], False),
    ("heavy bleeding,severe bleeding,bleeding wont stop", [EMER, HEM], False),
    # --- oncology ---
    ("cancer,tumor,tumour,lump,chemotherapy,chemo,biopsy,malignant,oncology", [ONC], False),
    ("brain tumor,brain tumour", [ONC, NEURO], False),
    # --- emergency red flags ---
    ("unconscious,unresponsive,not responding,collapsed,passed out,poison,poisoning,overdose,choking,"
     "drowning,electric shock,snake bite,dog bite,gunshot,stab,head injury,road accident", [EMER], False),
    ("accident,injury,injured,burns", [EMER, ORTHO], False),

    # ================= additional coverage =================
    # mood
    ("feeling low,feel low,feeling down,feel down,feeling sad,feel sad,no interest,lost interest,loss of interest,"
     "no motivation,worthless,crying spells,irritable,irritability,lonely,loneliness,burnout,overthinking,"
     "feeling empty,mood swing", [PSY, PSYCH], False),
    # eyes / allergy overlap
    ("itchy eyes,itching eyes,eye itching,eye allergy,sore eyes,eye irritation,dry eyes,eyes itch,eye itch", [OPH, ALLERGY], False),
    # limb weakness / walking problems
    ("weakness ~ leg,weakness ~ arm,weakness ~ hand,leg weakness,arm weakness,difficulty walking,difficulty ~ walking,"
     "trouble walking,cant walk,unable to walk,limping,limp,foot drop,balance problem,loss of balance", [NEURO, ORTHO], False),
    # stroke-type red flags
    ("weakness ~ side,one sided weakness,one side weakness,facial droop,face drooping,drooping face,slurred speech,"
     "slurring,trouble speaking,difficulty speaking,sudden confusion,sudden numbness", [EMER, NEURO], False),
    # bites / wounds / bleeding that will not stop
    ("snake ~ bit,snake ~ bite,dog ~ bit,dog ~ bite,cat ~ bit,monkey ~ bit,animal bite,bitten by ~ snake,bitten by ~ dog,"
     "rabies,deep cut,deep wound,wound,laceration,wont stop bleeding,will not stop bleeding,not stop bleeding,"
     "not stopping bleeding,cant stop bleeding,bleeding wont stop,profuse bleeding,bleeding profusely,bleeding a lot,"
     "head trauma,fell from height,fall from height,fell down ~ stairs,serious accident", [EMER], False),
    # jaundice style wording
    ("skin ~ yellow,yellow ~ skin,yellowish,yellowing,turning yellow", [GASTRO, HEP], False),
    ("dark urine", [GASTRO, HEP, URO], False),
    # small body parts + gout
    ("big toe,toe,finger,thumb,hand,thigh,calf,palm,heel pain,plantar,flat feet,foot pain", [ORTHO], True),
    ("swelling ~ toe,swollen ~ toe,swelling ~ big toe", [RHEUM, ORTHO], False),
    # hair / weight / thyroid-type wording
    ("thinning hair,hair ~ thinning,thinning of hair", [DERM, ENDO], False),
    ("gained weight,put on weight,weight increase,obese,obesity,overweight,lose weight,cant lose weight", [ENDO, GM], False),
    ("feel cold,feeling cold,always cold,sensitive to cold,cold intolerance", [ENDO], False),
    ("thirsty,excessive thirst,increased thirst,polyuria", [ENDO], False),
    # ENT extras
    ("lost ~ smell,loss of smell,sense of smell,cant smell,loss of taste,lost ~ taste,hoarse voice,hoarseness,"
     "voice change,loss of voice,earwax,wax in ear,ear wax", [ENT, ID], False),
    # dental extras
    ("bad breath,mouth ulcer,mouth ulcers,bleeding gums,gums bleed,swollen gums,gum bleeding,mouth sore,tooth decay,braces", [DENT], False),
    # fainting / cardiac wording
    ("faint,fainting,fainted,blackout,black out,syncope", [NEURO, CARD], False),
    ("skipping beats,skipped beat,missed beat", [CARD], False),
    ("heaviness in chest,chest heaviness,heavy chest,chest ~ heavy", [CARD, EMER], False),
    # kidney-type swelling
    ("swollen legs,swollen feet,swollen ankles,swelling ~ legs,swelling ~ feet,leg swelling,foamy urine", [NEPH, CARD, URO], False),
    # bones / vitamins
    ("osteoporosis,calcium deficiency,vitamin d,vitamin d deficiency", [ORTHO, ENDO], False),
    ("vitamin b12,b12,iron deficiency", [HEM, GM], False),

    # ================= lexicon expansion, round 2 (by body system) =================
    ("cramps", [GYN, GASTRO], True),
    ("leg cramps,muscle cramps,calf cramps,cramps ~ leg,cramps ~ legs,restless legs,muscle spasm,muscle spasms", [ORTHO, NEURO, GM], False),
    # neurology / mental
    ("burning sensation ~ feet,burning feet,burning ~ feet,burning ~ hands,neuropathy", [NEURO, ENDO], False),
    ("unable to concentrate,cant concentrate,cannot concentrate,poor concentration,restless,restlessness,agitated,agitation", [PSY, PSYCH], False),
    ("worst headache,sudden severe headache,sudden ~ headache,thunderclap", [EMER, NEURO], False),
    ("forgets,disoriented,disorientation,gets lost,wandering", [NEURO], False),
    # eyes
    ("double vision,see double,seeing double,diplopia,floaters,flashes of light,night blindness,squint,eye injury,injured eye", [OPH], False),
    # gastro
    ("pass ~ stool,passing ~ stool,hard stool,hard stools,no bowel movement,bowel,bowels,stool", [GASTRO], False),
    ("vomit blood,vomiting blood,vomit ~ blood,vomited blood,hematemesis,black stool,black stools,tarry stool", [EMER, GASTRO], False),
    ("hiccup,hiccups,belching,burping,gassy,flatulence,upset stomach,stomach upset,food poisoning,gas", [GASTRO, GM], False),
    # skin / hair / nails
    ("spots,dark spots,pigmentation,melasma,vitiligo,white patches,oily skin,dry skin,flaky skin,peeling,scalp,"
     "nail,nails,nail fungus,nail infection,sunburn,corn,lice,stretch marks,scar,keloid,bumps,dryness", [DERM], False),
    ("butterfly rash,malar rash", [RHEUM, DERM], False),
    # mouth / tongue / jaw
    ("tongue,tongue ulcer,blister ~ tongue,tongue blister,white patches ~ tongue", [DENT, ENT], False),
    ("jaw", [DENT, ENT], True),
    ("cant open ~ mouth,unable to open ~ mouth,lockjaw,tmj,tmj pain,jaw swelling,jaw locked", [DENT, ENT], False),
    # ENT / sleep / hearing / lymph
    ("stops breathing,stop breathing,sleep apnea,sleep apnoea,apnea,apnoea,loud snoring", [PULM, ENT], False),
    ("trouble hearing,difficulty hearing,hearing problem,hearing difficulty,not hearing,hearing,cannot hear", [ENT], False),
    ("swollen glands,lymph node,lymph nodes,swollen lymph,enlarged lymph,glands", [GM, ONC, ENT], False),
    # chest / breathing wording
    ("pain ~ breathing,pain ~ breathe,pain ~ deep breath,pleuritic,pain on inhaling,pain on breathing", [PULM, CARD], False),
    ("cant lie flat,cannot lie flat,orthopnea,breathless ~ night,breathless at night", [CARD, PULM], False),
    ("cough ~ blood,coughing ~ blood,spitting blood,spit blood,hemoptysis,blood ~ cough,blood in cough", [PULM, EMER], False),
    # emergencies / ingestion / injuries
    ("swallowed ~ poison,swallowed ~ poisonous,swallowed something,swallowed ~ chemical,swallowed ~ battery,swallowed ~ coin,"
     "poisonous,drank ~ poison,ate ~ poison,pesticide,acid attack,chemical ~ eye,trauma,"
     "nail ~ stuck,stepped on ~ nail,rusty nail", [EMER], False),
    # fertility
    ("trying to conceive,conceive,conception,infertility,infertile,fertility,ivf,sperm count,low sperm", [GYN, URO, ENDO], False),
    # routine care
    ("checkup,check up,health checkup,annual checkup,routine checkup,fitness certificate,medical certificate", [GM], True),

    # ================= round 3 =================
    ("hit ~ head,blow to ~ head,hit on ~ head,struck ~ head,head ~ hit,drowsy after,vomiting after ~ head", [EMER, NEURO], False),
    ("varicose,varicose veins,vein,veins,heavy legs,legs feel heavy,leg heaviness,swollen veins", [CARD, GM], False),
    ("breath smells,breath smell,smelly breath,foul breath,breath ~ smell", [DENT], False),
    ("chronic cough,persistent cough,long standing cough,smoker,smoking,smokers cough,tobacco", [PULM], False),
    # --- children (handled mostly via context, but keep explicit words) ---
    ("pediatric,paediatric,vaccination,vaccine,immunization", [PED], False),
]

_DETERMINERS = r"\b(?:my|the|your|his|her|our|their|a|an|some|very|really|so|too|extremely|quite)\b"
_PEDIATRIC_WORDS = {"baby", "babies", "infant", "newborn", "toddler", "child", "children", "kid", "kids"}
_NEGATION_CUES = {"no", "not", "without", "never", "denies", "dont", "doesnt", "didnt", "havent", "hasnt"}
_CLAUSE_SPLIT = re.compile(r"[,;.]|\bbut\b|\bhowever\b|\balthough\b|\bthough\b")
_AGE_RE = re.compile(r"\b(\d{1,2})\s*(?:year|yr)s?\s*old\b")
_MONTH_AGE_RE = re.compile(r"\b\d{1,2}\s*months?\s*old\b")

_WEIGHTS = (3.0, 2.0, 1.0)
_PEDIATRIC_BOOST = 4.0


def _norm(text):
    s = text.lower().replace("\u2019", "'")
    s = re.sub(r"'s\b", "", s)
    s = s.replace("'", "")
    s = re.sub(r"[^a-z0-9 ,;.]+", " ", s)
    s = re.sub(_DETERMINERS, " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Phrases that always raise the "seek urgent help" notice, whatever department ranks first.
_SELF_HARM_PHRASES = {_norm(p) for p in ("suicidal", "want to die", "self harm", "kill myself")}
_ALWAYS_URGENT_PHRASES = _SELF_HARM_PHRASES | {
    _norm(p) for p in ("chest pain", "heart attack", "chest tightness", "chest pressure",
                       "heaviness in chest", "chest heaviness", "difficulty breathing")
}

# Departments that are not seeded in the Department table are folded into the closest
# one that is, so the "Book Appointment" link always finds doctors.
_DEPT_ALIAS = {PSYCH: PSY, OBS: GYN}
_WILD = r"(?:[a-z]+\s+){0,3}?"   # "~" in a phrase = up to 3 filler words


def _canon(depts):
    out = []
    for d in depts:
        d = _DEPT_ALIAS.get(d, d)
        if d not in out:
            out.append(d)
    return out


def _build_patterns():
    patterns = []  # (compiled regex, phrase, depts, weak, nwords)
    seen = set()
    for phrases, depts, weak in _ENTRIES:
        depts = _canon(depts)
        for phrase in phrases.split(","):
            p = _norm(phrase.replace("~", " zzwild "))
            if not p:
                continue
            tokens = p.split()
            out, pending = "", False
            for i, w in enumerate(tokens):
                if w == "zzwild":
                    pending = True
                    continue
                tok = re.escape(w)
                if len(w) >= 3:
                    tok += r"(?:s|es|ed|ing|d)?"
                out = tok if not out else out + r"\s+" + (_WILD if pending else "") + tok
                pending = False
            key = (p, tuple(depts))
            if key in seen:
                continue
            seen.add(key)
            nwords = len([t for t in tokens if t != "zzwild"])
            patterns.append((re.compile(r"\b" + out + r"\b"), p.replace("zzwild", "~"), depts, weak, nwords))
    return patterns


_PATTERNS = _build_patterns()

# Public mapping kept for backward compatibility / documentation.
SYMPTOM_DEPARTMENT_MAP = {}
for _rx, _p, _d, _w, _n in _PATTERNS:
    SYMPTOM_DEPARTMENT_MAP.setdefault(_p, list(_d))

_COMMON_WORDS = {
    "about", "after", "again", "always", "anything", "around", "because", "before", "being", "between",
    "constantly", "continuously", "during", "every", "everything", "feel", "feels", "frequent", "frequently",
    "morning", "night", "nights", "really", "severe", "since", "something", "sometimes", "suddenly", "think",
    "there", "these", "those", "through", "under", "until", "where", "while", "years", "yesterday", "problem",
    "problems", "other", "people", "start", "started", "doctor", "hospital", "medicine", "appointment",
}

_FUZZY_VOCAB = sorted({p for _rx, p, _d, w, n in _PATTERNS if n == 1 and len(p) >= 5 and not w})
_FUZZY_INDEX = {}
for _rx, _p, _d, _w, _n in _PATTERNS:
    if _n == 1 and len(_p) >= 5 and not _w:
        _FUZZY_INDEX.setdefault(_p, (_d, _w, _n))


def _is_negated(text, start):
    clause = _CLAUSE_SPLIT.split(text[:start])[-1]
    window = clause.split()[-4:]
    return any(tok in _NEGATION_CUES for tok in window)


def _collect_matches(text):
    candidates = []
    for rx, phrase, depts, weak, nwords in _PATTERNS:
        for m in rx.finditer(text):
            candidates.append((m.start(), m.end(), phrase, depts, weak, nwords))
    # longest match first, so specific phrases consume generic words
    candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0]))
    taken, accepted = [], []
    for s, e, phrase, depts, weak, nwords in candidates:
        if any(s < te and e > ts for ts, te in taken):
            continue
        taken.append((s, e))
        accepted.append((s, e, phrase, depts, weak, nwords))
    # typo tolerance on leftover long words
    for m in re.finditer(r"[a-z]{5,}", text):
        s, e = m.span()
        if any(s < te and e > ts for ts, te in taken):
            continue
        word = m.group()
        if word.endswith("ing") or word in _COMMON_WORDS:
            continue  # ordinary verbs/adverbs: never "correct" them into symptoms
        close = [c for c in get_close_matches(word, _FUZZY_VOCAB, n=3, cutoff=0.88) if c[0] == word[0]]
        if close:
            depts, weak, nwords = _FUZZY_INDEX[close[0]]
            taken.append((s, e))
            accepted.append((s, e, close[0], depts, weak, nwords))
    accepted.sort(key=lambda a: a[0])
    return accepted


def _pediatric_context(raw_norm):
    tokens = set(re.findall(r"[a-z]+", raw_norm))
    if tokens & _PEDIATRIC_WORDS:
        return True
    if _MONTH_AGE_RE.search(raw_norm):
        return True
    m = _AGE_RE.search(raw_norm)
    return bool(m and int(m.group(1)) <= 12)


def analyze_symptoms(symptoms, top_n=3):
    """
    Full analysis of a symptom description.

    Returns a dict:
      primary       best department
      alternatives  other plausible departments (best first, up to top_n - 1)
      matched       symptom phrases that were recognised
      recognised    False when nothing matched and General Medicine is only a default
      urgent        True when an emergency red flag was recognised
      self_harm     True when the text mentions suicide / self-harm
    """
    if not symptoms or not symptoms.strip():
        return {"primary": GM, "alternatives": [], "matched": [], "recognised": False, "urgent": False, "self_harm": False}

    text = _norm(symptoms)
    scores, first_seen, matched = {}, {}, []
    urgent = False
    self_harm = False
    used = set()
    for s, e, phrase, depts, weak, nwords in _collect_matches(text):
        if _is_negated(text, s):
            continue
        if phrase in used:
            continue
        used.add(phrase)
        matched.append(phrase)
        if depts[0] == EMER or phrase in _ALWAYS_URGENT_PHRASES:
            urgent = True
        if phrase in _SELF_HARM_PHRASES:
            self_harm = True
        mult = (0.5 if weak else 1.0) * (1 + 0.2 * (nwords - 1))
        for i, dept in enumerate(depts[:3]):
            scores[dept] = scores.get(dept, 0.0) + _WEIGHTS[i] * mult
            first_seen.setdefault(dept, s)
    if _pediatric_context(text):
        scores[PED] = scores.get(PED, 0.0) + _PEDIATRIC_BOOST
        first_seen.setdefault(PED, -1)
        matched.append("child / infant")

    if not scores:
        return {"primary": GM, "alternatives": [], "matched": [], "recognised": False, "urgent": False, "self_harm": False}

    ranked = sorted(scores, key=lambda d: (-scores[d], first_seen[d]))[:top_n]
    if ranked[0] == EMER:
        urgent = True
    return {
        "primary": ranked[0],
        "alternatives": ranked[1:],
        "matched": matched,
        "recognised": True,
        "urgent": urgent,
        "self_harm": self_harm,
    }


def rank_departments(symptoms, top_n=3):
    """Up to top_n department names, best first. Falls back to General Medicine."""
    r = analyze_symptoms(symptoms, top_n)
    return [r["primary"]] + r["alternatives"]


def suggest_department(symptoms):
    """Single best department (drop-in replacement for the original function)."""
    return analyze_symptoms(symptoms, 1)["primary"]