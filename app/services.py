"""Service catalogue: what people can ask for, how it is priced, and how to find providers on a map.

goal = "negotiate"   → the agent bargains for the best all-inclusive price (cab, cook, plumber…)
goal = "appointment" → the agent books a slot and asks the fee; no haggling (clinic, dentist, salon…)
Anything not in the catalogue becomes a generic "other" service with the user's own words as its label.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional


@dataclass(frozen=True)
class ServiceType:
    key: str
    label: str
    keywords: tuple
    goal: str = "negotiate"
    unit: str = "flat"                      # flat | per_hour | per_day | per_month | per_visit | per_km
    typical: tuple = (500, 1500)            # rough Bengaluru price range in ₹ (for budget suggestions + sims)
    maps_query: str = ""
    osm_tags: tuple = ()                    # (key, value) pairs for OpenStreetMap
    who: str = "vendor"                     # "another {who} nearby quoted…"
    addon_label: str = "extra charges"
    detail_question: str = ""
    transport: bool = False
    extras: dict = field(default_factory=dict)


SERVICES: tuple[ServiceType, ...] = (
    ServiceType("cab_outstation", "cab / taxi", ("cab", "taxi", "innova", "ertiga", "car rental", "outstation",
                "airport drop", "airport pickup", "driver", "suv", "7-seater", "7 seater", "tempo traveller"),
                unit="flat", typical=(1500, 4000), maps_query="taxi service", osm_tags=(("amenity", "taxi"),
                ("office", "taxi")), who="operator", addon_label="parking / night charge", transport=True,
                detail_question="Which vehicle (sedan, SUV, 7-seater), and how many passengers?"),
    ServiceType("mini_truck", "packers & movers / mini truck", ("shift", "shifting", "movers", "packers",
                "tempo", "mini truck", "relocat", "house move", "1bhk", "2bhk", "3bhk"), typical=(3000, 8000),
                maps_query="packers and movers", osm_tags=(("office", "moving_company"), ("shop", "removals")),
                who="mover", addon_label="loading-unloading labour", transport=True,
                detail_question="How much stuff (1BHK, 2BHK, a few items), and which floor?"),
    ServiceType("ac_repair", "AC repair / service", ("ac ", "a/c", "air condition", "ac repair", "ac service",
                "not cooling", "ac gas"), typical=(500, 2000), maps_query="AC repair service",
                osm_tags=(("craft", "hvac"),), who="technician", addon_label="gas refill / parts",
                detail_question="What's wrong with the AC (not cooling, noise, service) and which type (split/window)?"),
    ServiceType("home_cook", "home cook", ("cook", "chef", "khana", "cooking", "rasoiya", "maharaj", "tiffin"),
                unit="per_month", typical=(6000, 15000), maps_query="home cook service",
                osm_tags=(("office", "employment_agency"),), who="cook", addon_label="travel / festival extras",
                detail_question="How many people, veg or non-veg, and how many meals a day?"),
    ServiceType("maid", "house maid / cleaning help", ("maid", "bai", "house help", "housekeeping", "cleaning lady",
                "domestic help", "jhadu", "pocha"), unit="per_month", typical=(3000, 9000),
                maps_query="maid service agency", osm_tags=(("office", "employment_agency"),), who="agency",
                addon_label="agency fee / extras",
                detail_question="What work (sweeping, mopping, dishes, laundry) and how many hours a day?"),
    ServiceType("deep_cleaning", "home deep cleaning", ("deep clean", "home cleaning", "sofa clean",
                "bathroom clean", "kitchen clean", "cleaning service"), typical=(2000, 6000),
                maps_query="home deep cleaning service", osm_tags=(("shop", "cleaning"),), who="cleaner",
                detail_question="Which rooms or items, and how big is the home (1BHK/2BHK/3BHK)?"),
    ServiceType("plumber", "plumber", ("plumber", "plumbing", "leak", "tap", "pipe", "drainage", "toilet block",
                "water tank"), typical=(300, 1500), maps_query="plumber", osm_tags=(("craft", "plumber"),),
                who="plumber", addon_label="material / parts",
                detail_question="What's the problem (leak, blockage, new fitting)?"),
    ServiceType("electrician", "electrician", ("electrician", "wiring", "switch", "fan install", "short circuit",
                "light fitting", "mcb", "inverter"), typical=(300, 1500), maps_query="electrician",
                osm_tags=(("craft", "electrician"),), who="electrician", addon_label="material / parts",
                detail_question="What needs fixing or installing?"),
    ServiceType("carpenter", "carpenter", ("carpenter", "furniture repair", "wardrobe", "door repair", "woodwork",
                "bed repair"), typical=(500, 3000), maps_query="carpenter", osm_tags=(("craft", "carpenter"),),
                who="carpenter", addon_label="material", detail_question="What work is needed?"),
    ServiceType("painter", "house painter", ("paint", "painter", "painting", "wall paint", "whitewash"),
                typical=(8000, 30000), maps_query="house painting service", osm_tags=(("craft", "painter"),),
                who="painter", addon_label="paint / material",
                detail_question="How many rooms or square feet, and interior or exterior?"),
    ServiceType("pest_control", "pest control", ("pest", "cockroach", "termite", "bed bug", "rats", "mosquito"),
                typical=(800, 3000), maps_query="pest control service", osm_tags=(("shop", "pest_control"),),
                who="pest control service", detail_question="Which pest, and how big is the home?"),
    ServiceType("appliance_repair", "appliance repair", ("washing machine", "fridge", "refrigerator", "microwave",
                "geyser", "ro repair", "water purifier", "tv repair", "appliance"), typical=(300, 2000),
                maps_query="home appliance repair", osm_tags=(("craft", "electronics_repair"),
                ("shop", "electronics")), who="technician", addon_label="parts",
                detail_question="Which appliance and what's the problem?"),
    ServiceType("bike_mechanic", "bike / scooter mechanic", ("bike repair", "scooter", "two wheeler", "bike service",
                "puncture", "activa"), typical=(300, 1500), maps_query="two wheeler service center",
                osm_tags=(("shop", "motorcycle_repair"), ("shop", "motorcycle")), who="mechanic",
                addon_label="parts", detail_question="Which bike and what's the issue?"),
    ServiceType("car_service", "car mechanic / service", ("car repair", "car service", "mechanic", "garage",
                "car wash", "battery jump"), typical=(1500, 6000), maps_query="car service center",
                osm_tags=(("shop", "car_repair"),), who="garage", addon_label="parts / oil",
                detail_question="Which car and what does it need?"),
    ServiceType("doctor_clinic", "doctor / clinic", ("doctor", "clinic", "physician", "fever", "consult",
                "check up", "checkup", "gp ", "pediatric", "paediatric", "gynaec", "dermatolog", "skin doctor",
                "ent ", "ortho", "hospital"), goal="appointment", unit="per_visit", typical=(300, 1000),
                maps_query="clinic", osm_tags=(("amenity", "clinic"), ("amenity", "doctors")), who="clinic",
                detail_question="What's the issue or which kind of doctor (general, child, skin…)?"),
    ServiceType("dentist", "dentist", ("dentist", "dental", "tooth", "teeth", "root canal", "braces"),
                goal="appointment", unit="per_visit", typical=(300, 1500), maps_query="dental clinic",
                osm_tags=(("amenity", "dentist"),), who="dental clinic",
                detail_question="Is it a check-up, pain, cleaning or something else?"),
    ServiceType("physiotherapist", "physiotherapist", ("physio", "back pain", "knee pain", "rehab"),
                goal="appointment", unit="per_visit", typical=(500, 1200), maps_query="physiotherapy clinic",
                osm_tags=(("healthcare", "physiotherapist"),), who="physio clinic",
                detail_question="What's the problem, and clinic visit or home visit?"),
    ServiceType("diagnostic_lab", "diagnostic lab / blood test", ("blood test", "lab test", "diagnostic",
                "pathology", "scan", "x-ray", "xray", "full body checkup"), goal="appointment", unit="per_visit",
                typical=(500, 3000), maps_query="diagnostic lab", osm_tags=(("healthcare", "laboratory"),),
                who="lab", detail_question="Which tests, and home sample collection or lab visit?"),
    ServiceType("vet", "veterinary clinic", ("vet", "veterinary", "dog doctor", "cat doctor", "pet clinic"),
                goal="appointment", unit="per_visit", typical=(400, 1200), maps_query="veterinary clinic",
                osm_tags=(("amenity", "veterinary"),), who="vet clinic", detail_question="Which pet and what's wrong?"),
    ServiceType("salon", "salon / beauty parlour", ("salon", "haircut", "hair cut", "parlour", "parlor", "facial",
                "beautician", "spa", "manicure", "bridal makeup", "makeup"), goal="appointment", unit="per_visit",
                typical=(300, 3000), maps_query="salon", osm_tags=(("shop", "hairdresser"), ("shop", "beauty")),
                who="salon", detail_question="Which service (haircut, facial, makeup…), and at the salon or at home?"),
    ServiceType("tutor", "home tutor", ("tutor", "tuition", "teacher", "coaching", "maths class", "classes for"),
                unit="per_month", typical=(3000, 10000), maps_query="home tutor", osm_tags=(("amenity", "school"),),
                who="tutor", detail_question="Which class/subject, and how many days a week?"),
    ServiceType("fitness", "yoga / fitness trainer", ("yoga", "gym", "fitness", "personal trainer", "zumba"),
                unit="per_month", typical=(2000, 8000), maps_query="yoga classes", osm_tags=(("leisure",
                "fitness_centre"),), who="trainer", detail_question="Yoga or gym/personal training, and at home or a centre?"),
    ServiceType("caterer", "caterer", ("caterer", "catering", "party food", "bulk food"), typical=(15000, 60000),
                maps_query="caterers", osm_tags=(("craft", "caterer"),), who="caterer",
                detail_question="How many guests, veg/non-veg, and what kind of event?"),
    ServiceType("photographer", "photographer", ("photographer", "photoshoot", "photo shoot", "videographer"),
                typical=(5000, 30000), maps_query="photographer", osm_tags=(("craft", "photographer"),),
                who="photographer", detail_question="What event and for how many hours?"),
    ServiceType("event_decorator", "event decorator", ("decorat", "balloon", "birthday decoration",
                "event planner"), typical=(3000, 20000), maps_query="event decorators",
                osm_tags=(("shop", "party"),), who="decorator", detail_question="Which event, venue and theme?"),
    ServiceType("laundry", "laundry / dry cleaning", ("laundry", "dry clean", "ironing", "press", "wash and fold"),
                typical=(200, 1000), maps_query="laundry service", osm_tags=(("shop", "laundry"),
                ("shop", "dry_cleaning")), who="laundry", detail_question="How many clothes, and pickup from home?"),
    ServiceType("tailor", "tailor", ("tailor", "stitch", "alteration", "blouse"), typical=(300, 2000),
                maps_query="tailor", osm_tags=(("craft", "tailor"), ("shop", "tailor")), who="tailor",
                detail_question="What needs stitching or altering?"),
    ServiceType("babysitter", "babysitter / nanny", ("babysit", "nanny", "child care", "creche", "daycare"),
                unit="per_month", typical=(8000, 20000), maps_query="nanny services",
                osm_tags=(("amenity", "childcare"),), who="agency", detail_question="Child's age and hours needed?"),
    ServiceType("elder_care", "nurse / elder care attendant", ("nurse", "attendant", "elder care", "caretaker",
                "patient care"), unit="per_month", typical=(15000, 30000), maps_query="home nursing services",
                osm_tags=(("healthcare", "nurse"),), who="agency", detail_question="Day, night or 24-hour care, and any medical needs?"),
    ServiceType("legal", "lawyer", ("lawyer", "advocate", "legal", "notary", "rent agreement"),
                goal="appointment", unit="per_visit", typical=(1000, 5000), maps_query="lawyer",
                osm_tags=(("office", "lawyer"),), who="lawyer", detail_question="What's the legal matter?"),
    ServiceType("accountant", "CA / tax consultant", ("chartered accountant", " ca ", "tax filing", "itr", "gst",
                "accountant"), goal="appointment", unit="per_visit", typical=(1000, 5000),
                maps_query="chartered accountant", osm_tags=(("office", "accountant"),), who="CA office",
                detail_question="What do you need (ITR, GST, company filing)?"),
    ServiceType("gardener", "gardener", ("garden", "gardener", "mali", "lawn", "plants", "landscap"),
                typical=(500, 2500), maps_query="gardening service", osm_tags=(("shop", "garden_centre"),),
                who="gardener", detail_question="What needs doing (maintenance, new plants, terrace garden setup)?"),
    ServiceType("locksmith", "locksmith / key maker", ("locksmith", "key maker", "duplicate key", "locked out",
                "lock repair"), typical=(200, 1000), maps_query="locksmith", osm_tags=(("shop", "locksmith"),
                ("craft", "key_cutter")), who="locksmith", detail_question="Locked out, new lock, or duplicate key?"),
    ServiceType("phone_repair", "mobile / laptop repair", ("phone repair", "mobile repair", "screen broken",
                "laptop repair", "computer repair", "battery replace"), typical=(500, 4000),
                maps_query="mobile phone repair", osm_tags=(("shop", "mobile_phone"), ("shop", "computer")),
                who="repair shop", addon_label="parts", detail_question="Which device and what's broken?"),
    ServiceType("driver", "driver on hire", ("hire a driver", "driver for", "acting driver", "personal driver"),
                unit="per_day", typical=(800, 1500), maps_query="driver on hire service", who="driver agency",
                transport=False, detail_question="For how many hours/days, and which car?"),
    ServiceType("water_tanker", "water tanker", ("water tanker", "tanker", "water supply"), typical=(800, 2000),
                maps_query="water tanker supplier", osm_tags=(("shop", "water"),), who="supplier",
                detail_question="How many litres, and what's the address type (apartment/house)?"),
    ServiceType("pet_care", "pet groomer / sitter", ("pet groom", "dog walk", "pet sitter", "dog groom",
                "boarding"), typical=(500, 2000), maps_query="pet grooming", osm_tags=(("shop", "pet_grooming"),),
                who="pet care", detail_question="Which pet, and grooming, walking or boarding?"),
    ServiceType("priest", "pandit / priest", ("pandit", "pooja", "puja", "priest", "havan", "griha pravesh"),
                typical=(1500, 6000), maps_query="pandit for pooja", osm_tags=(("amenity", "place_of_worship"),),
                who="pandit ji", detail_question="Which pooja, and how many hours?"),
    ServiceType("courier", "courier / delivery", ("courier", "parcel", "send package", "delivery boy"),
                typical=(100, 800), maps_query="courier service", osm_tags=(("office", "courier"),),
                who="courier", transport=True, detail_question="What are you sending, and roughly how big or heavy?"),
)

BY_KEY = {s.key: s for s in SERVICES}
OTHER = ServiceType("other", "local service", (), maps_query="", who="vendor")


def get(key: Optional[str], label: str = "") -> ServiceType:
    if key in BY_KEY:
        return BY_KEY[key]
    clean = (label or "").strip() or OTHER.label
    return replace(OTHER, label=clean, maps_query=clean)


def detect(text: str) -> Optional[ServiceType]:
    """Best keyword match, longest keyword wins (so 'ac repair' beats 'repair')."""
    t = f" {text.lower()} "
    best, best_len = None, 0
    for svc in SERVICES:
        for kw in svc.keywords:
            if kw in t and len(kw) > best_len:
                best, best_len = svc, len(kw)
    return best


def catalogue() -> list[dict]:
    return [{"key": s.key, "label": s.label, "goal": s.goal, "unit": s.unit, "typical": list(s.typical)}
            for s in SERVICES]


UNIT_WORDS = {"per_month": "per month", "per_hour": "per hour", "per_day": "per day", "per_visit": "per visit",
              "per_km": "per km", "flat": ""}


def suggested_budget(svc: ServiceType) -> tuple[int, int]:
    low, high = svc.typical
    target = int(round((low + (high - low) * 0.35) / 50) * 50)
    return target, int(high)

