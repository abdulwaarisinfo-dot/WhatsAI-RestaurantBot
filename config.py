"""
config.py — Environment variables, constants, and global state containers
WhatsApp AI Restaurant Bot v14.7 + Table Reservations
"""

import os
import logging
from typing import Dict, List, Any
from collections import defaultdict
from dotenv import load_dotenv
from langdetect import DetectorFactory

load_dotenv()
DetectorFactory.seed = 0

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RestaurantBot.v14.7")

# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

MONGO_URI         = os.getenv("MONGO_URI", "")
WHATSAPP_TOKEN    = os.getenv("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN", "my_verify_token")
SECRET_PASSWORD   = os.getenv("SECRET_PASSWORD", "admin")
CRM_USERNAME      = os.getenv("USER_NAME", "admin")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

WHATSAPP_API_URL = f"https://graph.facebook.com/v25.0/{WHATSAPP_PHONE_ID}/messages"

RATE_LIMIT_PER_MINUTE = 15

# ============================================================
# GLOBAL STATE
# ============================================================

BOT_DATA: Dict[str, Any] = {}
PRODUCTS_DATA: List[Dict[str, Any]] = []
PRODUCT_KEYWORD_INDEX: Dict[str, List[Dict]] = {}
USER_SESSIONS: Dict[str, Dict[str, Any]] = {}
_rate_store: Dict[str, list] = defaultdict(list)

# ============================================================
# TABLE RESERVATION CONSTANTS
# ============================================================

# Valid time slots for reservations (24h format strings)
RESERVATION_TIME_SLOTS = [
    "12:00", "12:30", "13:00", "13:30", "14:00", "14:30",
    "15:00", "15:30", "16:00", "16:30", "17:00", "17:30",
    "18:00", "18:30", "19:00", "19:30", "20:00", "20:30",
    "21:00", "21:30", "22:00",
]

# Maximum guests per reservation
RESERVATION_MAX_GUESTS = 20

# Minimum advance booking in hours
RESERVATION_MIN_ADVANCE_HOURS = 1

# Reservation statuses
RESERVATION_STATUSES = ["Pending", "Confirmed", "Cancelled", "Completed", "No Show"]

# Reservation flow steps (negative to avoid clash with order steps)
STEP_RESERVATION_NAME     = -1
STEP_RESERVATION_DATE     = -2
STEP_RESERVATION_TIME     = -3
STEP_RESERVATION_GUESTS   = -4
STEP_RESERVATION_NOTES    = -5
STEP_RESERVATION_CONFIRM  = -6

# ============================================================
# KEYWORD DATABASES
# ============================================================

CATEGORY_KEYWORDS = {
    "burger":   [
        "burger", "برگر", "brgr", "cheeseburger", "double burger", "zinger",
        "chicken burger", "beef burger", "smash burger", "crispy burger",
    ],
    "pizza":    [
        "pizza", "پیزا", "پیزہ", "margherita", "pepperoni", "pizza slice",
        "tikka pizza", "bbq pizza", "piza",
    ],
    "biryani":  [
        "biryani", "بریانی", "dum biryani", "chicken biryani", "beef biryani",
        "baryani", "biriyani", "briyani", "biryaani",
    ],
    "drinks":   [
        "drink", "مشروب", "juice", "cola", "water", "سافٹ ڈرنک", "lassi",
        "coke", "pepsi", "7up", "sprite", "fanta", "soda", "cold drink",
        "doodh", "milk", "chai", "tea", "coffee", "shake", "milkshake",
        "smoothie", "lemonade", "nimbu pani", "fresh juice", "cold coffee",
    ],
    "dessert":  [
        "dessert", "مٹھائی", "cake", "kheer", "halwa", "gulab jamun",
        "brownie", "meetha", "sweet", "mithai", "ice cream", "kulfi",
        "falooda", "rabri", "gajar halwa",
    ],
    "karahi":   [
        "karahi", "کڑاہی", "chicken karahi", "beef karahi", "mutton karahi",
        "karhai", "dum karahi", "karai", "kadai", "karahi gosht",
    ],
    "rice":     ["rice", "چاول", "pulao", "plov", "fried rice", "chawal", "zeera rice"],
    "rolls":    ["roll", "رول", "shawarma", "wrap", "paratha roll", "spring roll"],
    "chicken":  ["chicken", "چکن", "murgh", "murg", "grilled chicken", "fried chicken"],
    "beef":     ["beef", "گوشت", "gosht", "beef boti", "beef tikka"],
    "mutton":   ["mutton", "lamb", "دنبہ", "bhed"],
    "fish":     ["fish", "مچھلی", "seafood", "prawn", "machli"],
    "bread":    [
        "naan", "roti", "paratha", "bread", "روٹی", "نان", "chapati",
        "tandoori roti", "butter naan", "garlic naan",
    ],
    "shawarma": ["shawarma", "شوارمہ", "shwarma", "shaverma"],
    "pasta":    ["pasta", "پاستا", "spaghetti", "macaroni", "penne"],
    "sandwich": ["sandwich", "سینڈوچ", "sub", "club sandwich"],
    "soup":     ["soup", "شوربہ", "shorba", "yakhni"],
    "salad":    ["salad", "سلاد", "raita"],
    "steak":    ["steak", "اسٹیک", "grilled", "bbq steak"],
}

INTENT_KEYWORDS = {
    "discount":  ["discount", "sale", "deal", "offer", "cheap", "سستا", "رعایت", "rabatt",
                  "special offer", "promo", "coupon"],
    "order":     ["order", "آرڈر", "buy", "place order", "i want", "مجھے چاہیے", "bestellen",
                  "chahiye", "dena", "lena", "add", "mujhe", "give me", "i'll have",
                  "i'd like", "can i get", "get me", "send me", "bhai dena", "yaar dena",
                  "ek dena", "do dena", "lao", "manga", "mangwao", "order karo",
                  "order now", "order again", "want to order", "want"],
    "menu":      ["menu", "مینو", "menü", "what do you have", "show menu", "list", "items",
                  "all items", "show all", "kya hai", "kya milta", "aapke paas kya",
                  "what's available", "what do you serve", "show items", "menu dikhao",
                  "mujhe menu", "kya kya hai", "dekhna chahta"],
    "price":     ["price", "قیمت", "preis", "cost", "how much", "kitna", "rate",
                  "all prices", "all flavours", "all flavors", "price list", "rates",
                  "kitne ka", "kitni", "kya rate", "daam", "qeemat"],
    "greeting":  ["hi", "hello", "hey", "assalam", "السلام", "hallo", "guten tag", "سلام",
                  "start", "begin", "aoa", "aslam", "good morning", "good evening",
                  "good afternoon", "salam", "as salam", "walaikum"],
    "address":   ["address", "پتہ", "adresse", "location", "deliver to", "my address"],
    "status":    ["status", "where", "order status", "track", "delivered", "pending",
                  "where is my order", "track order", "mera order", "kahan hai order"],
    "cancel":    [
        "cancel", "منسوخ", "stornieren", "nahi chahiye", "remove order",
        "delete order", "hatao", "band karo", "order cancel", "cancel order",
        "delete", "remove", "clear order", "order delete", "order hatao",
        "order band", "mujhe nahi chahiye", "order mat karo",
        "order nahi", "order wapas", "order bhool jao",
    ],
    "cart":      ["cart", "basket", "my order", "show cart", "view cart", "what did i order",
                  "my cart", "mera cart", "meri basket"],
    "confirm":   ["confirm", "yes", "okay", "ok", "haan", "ہاں", "proceed", "place", "done",
                  "confirm order", "place order", "theek hai", "bilkul", "zaroor",
                  "sure", "absolutely", "go ahead", "finalize"],
    "clear":     ["clear cart", "empty cart", "start over", "restart", "reset cart"],
    "new_order": [
        "new order", "naya order", "start new", "nayi order", "fresh order",
        "order again", "reorder", "new aaorder", "new ordar", "new aorder",
        "order new", "nai order", "dobara order", "phir order", "again order",
    ],
    "show_total": [
        "tell me total", "show total", "my total", "mera total", "total kitna",
        "kitna total", "total kya", "abhi total", "total bta", "price total",
        "how much total", "total price", "total amount",
    ],
    "delivery_charge": [
        "delivery charge", "delivery fee", "delivery cost", "delivery kitna",
        "delivery charges", "kitna delivery", "free delivery", "delivery free",
        "ڈلیوری چارج", "ڈلیوری فیس", "liefergebühr",
    ],
    "inquiry": [
        "tell me about", "what is", "describe", "kya hai", "kaisa hai",
        "batao", "bataiye", "details", "more info", "information about",
        "what sizes", "what flavors", "what options", "kaunse size",
        "kya varieties", "available sizes",
    ],
    "thanks": [
        "thank", "thanks", "thankyou", "thank you", "shukriya", "شکریہ",
        "jazakallah", "jazak allah", "great", "awesome", "perfect", "excellent",
        "wonderful", "brilliant", "amazing",
    ],
    # ── NEW: Table reservation intents ──────────────────────────
    "reservation": [
        "reserve", "reservation", "book a table", "table booking", "book table",
        "table reserve", "seat", "dine in", "dine-in", "dining", "sit in",
        "ریزرویشن", "میز بک", "میز محفوظ", "tisch reservieren", "tisch buchen",
        "want a table", "need a table", "table for", "book for",
        "i want to reserve", "i want to book", "reserve table",
        "table reservation", "make a reservation", "get a table",
        "book a seat", "reserve a seat", "inside seating", "outdoor seating",
    ],
    "my_reservations": [
        "my reservation", "my booking", "my table", "show reservation",
        "view reservation", "check reservation", "reservation status",
        "meri reservation", "meri booking", "mera table",
        "cancel reservation", "cancel booking", "reservation cancel",
    ],
}

_CATEGORY_EMOJI_MAP = {
    "pizza":    "🍕",
    "burger":   "🍔",
    "biryani":  "🍛",
    "karahi":   "🥘",
    "drinks":   "🥤",
    "dessert":  "🍰",
    "rice":     "🍚",
    "rolls":    "🌯",
    "chicken":  "🍗",
    "beef":     "🥩",
    "mutton":   "🍖",
    "fish":     "🐟",
    "soup":     "🍲",
    "salad":    "🥗",
    "bread":    "🫓",
    "pasta":    "🍝",
    "steak":    "🥩",
    "shawarma": "🌯",
    "sandwich": "🥪",
}

EXAMPLE_MENU = {
    "pizza": [
        {"title": "Margherita Pizza",    "variants": [{"size": "Small", "price": 490}, {"size": "Medium", "price": 790}, {"size": "Large", "price": 1090}]},
        {"title": "BBQ Chicken Pizza",   "variants": [{"size": "Small", "price": 590}, {"size": "Medium", "price": 950}, {"size": "Large", "price": 1290}]},
        {"title": "Tikka Pizza",         "variants": [{"size": "Small", "price": 550}, {"size": "Medium", "price": 890}, {"size": "Large", "price": 1190}]},
        {"title": "Pepperoni Pizza",     "variants": [{"size": "Small", "price": 620}, {"size": "Medium", "price": 990}, {"size": "Large", "price": 1390}]},
    ],
    "burger": [
        {"title": "Zinger Burger",        "variants": [{"size": "Regular", "price": 320}, {"size": "Large", "price": 490}]},
        {"title": "Smash Burger",         "variants": [{"size": "Single", "price": 390}, {"size": "Double", "price": 590}]},
        {"title": "Cheese Burger",        "variants": [{"size": "Regular", "price": 290}, {"size": "Large", "price": 440}]},
        {"title": "Crispy Chicken Burger","variants": [{"size": "Regular", "price": 350}, {"size": "Large", "price": 520}]},
    ],
    "biryani": [
        {"title": "Chicken Biryani", "variants": [{"size": "Half Plate", "price": 320}, {"size": "Full Plate", "price": 590}, {"size": "Family Pack", "price": 1190}]},
        {"title": "Beef Biryani",    "variants": [{"size": "Half Plate", "price": 370}, {"size": "Full Plate", "price": 650}, {"size": "Family Pack", "price": 1290}]},
        {"title": "Sindhi Biryani",  "variants": [{"size": "Half Plate", "price": 340}, {"size": "Full Plate", "price": 620}, {"size": "Family Pack", "price": 1250}]},
    ],
    "karahi": [
        {"title": "Chicken Karahi", "variants": [{"size": "0.5kg", "price": 590}, {"size": "1kg", "price": 1090}]},
        {"title": "Beef Karahi",    "variants": [{"size": "0.5kg", "price": 690}, {"size": "1kg", "price": 1290}]},
        {"title": "Mutton Karahi",  "variants": [{"size": "0.5kg", "price": 890}, {"size": "1kg", "price": 1690}]},
    ],
    "drinks": [
        {"title": "Soft Drink",  "variants": [{"size": "Regular", "price": 80},  {"size": "Large", "price": 120}]},
        {"title": "Fresh Juice", "variants": [{"size": "Small",   "price": 150}, {"size": "Large", "price": 250}]},
        {"title": "Lassi",       "variants": [{"size": "Regular", "price": 120}, {"size": "Large", "price": 200}]},
        {"title": "Cold Coffee", "variants": [{"size": "Regular", "price": 180}, {"size": "Large", "price": 280}]},
    ],
    "dessert": [
        {"title": "Gulab Jamun", "variants": [{"size": "6 Pieces",     "price": 180}, {"size": "12 Pieces",    "price": 340}]},
        {"title": "Kheer",       "variants": [{"size": "Small",        "price": 120}, {"size": "Large",        "price": 220}]},
        {"title": "Ice Cream",   "variants": [{"size": "Single Scoop", "price": 90},  {"size": "Double Scoop", "price": 160}]},
    ],
}

_UNIVERSAL_CATEGORY_ALIASES = {
    "burger":  [
        "burger", "brgr", "برگر", "zinger", "cheeseburger", "double burger",
        "chicken burger", "beef burger", "crispy burger", "smash burger",
        "whopper", "mcburger", "spicy burger", "cheese burger",
    ],
    "pizza":   [
        "pizza", "پیزا", "پیزہ", "margherita", "pepperoni", "pizza slice",
        "tikka pizza", "bbq pizza", "pizza pie", "thin crust", "thick crust",
        "cheese pizza", "chicken pizza", "veggie pizza", "piza",
    ],
    "biryani": [
        "biryani", "بریانی", "baryani", "dum biryani", "chicken biryani",
        "beef biryani", "mutton biryani", "hyderabadi biryani", "sindhi biryani",
        "biryani rice", "biriyani", "biryaani", "briyani", "biriyan",
    ],
    "drinks":  [
        "drink", "مشروب", "cola", "juice", "coke", "pepsi", "lassi",
        "cold drink", "soda", "7up", "sprite", "fanta", "water", "سافٹ ڈرنک",
        "doodh", "milk", "chai", "tea", "coffee", "shake", "milkshake",
        "smoothie", "lemonade", "nimbu pani", "mango juice", "orange juice",
        "fresh juice", "cold coffee", "iced tea", "rooh afza",
    ],
    "dessert": [
        "dessert", "مٹھائی", "sweet", "cake", "kheer", "meetha",
        "halwa", "gulab jamun", "brownie", "mithai", "ice cream", "icecream",
        "kulfi", "rabri", "sewaiyan", "falooda", "pudding", "pastry",
        "muffin", "donut", "doughnut", "waffle", "gajar halwa",
    ],
    "karahi":  [
        "karahi", "کڑاہی", "karai", "karhai", "chicken karahi",
        "beef karahi", "mutton karahi", "dum karahi", "karahi gosht",
        "peshwari karahi", "balti", "kadai", "kadhai", "karai gosht",
    ],
    "rice":    [
        "rice", "چاول", "pulao", "fried rice", "plov", "chawal",
        "plain rice", "steamed rice", "zeera rice", "matar pulao",
        "kabuli pulao", "yakhni pulao",
    ],
    "rolls":   [
        "roll", "رول", "shawarma", "wrap", "paratha roll",
        "chicken roll", "beef roll", "seekh roll", "tikka roll",
        "spring roll", "egg roll",
    ],
    "chicken": [
        "chicken", "چکن", "murgh", "murg", "chkn",
        "grilled chicken", "fried chicken", "chicken pieces",
        "chicken tikka", "chicken boti", "chicken malai",
    ],
    "beef":    [
        "beef", "گوشت", "gosht", "gai ka gosht",
        "beef boti", "beef tikka", "beef seekh", "beaf",
    ],
    "mutton":  [
        "mutton", "lamb", "دنبہ", "bhed", "bakra",
        "mutton karahi", "mutton biryani", "mutton chops",
    ],
    "soup":    ["soup", "شوربہ", "shorba", "yakhni", "lentil soup", "dal soup"],
    "salad":   ["salad", "سلاد", "raita", "slaw"],
    "bread":   [
        "bread", "naan", "roti", "paratha", "روٹی", "نان", "پراٹھا",
        "chapati", "tandoori roti", "plain naan",
        "butter naan", "garlic naan", "puri", "bhatura",
    ],
    "shawarma": [
        "shawarma", "شوارمہ", "shwarma", "shaverma", "chicken shawarma",
        "beef shawarma",
    ],
    "sandwich": [
        "sandwich", "سینڈوچ", "sub", "club sandwich",
        "chicken sandwich", "grilled sandwich",
    ],
    "pasta":   [
        "pasta", "پاستا", "spaghetti", "macaroni", "penne",
        "fettuccine", "alfredo", "bolognese", "arabiata",
    ],
    "steak":   [
        "steak", "اسٹیک", "grilled", "bbq steak", "fillet",
        "beef steak", "chicken steak",
    ],
    "fish":    [
        "fish", "مچھلی", "seafood", "prawn", "shrimp",
        "fried fish", "grilled fish", "machli",
    ],
}

QUANTITY_WORDS = {
    "ek": 1, "one": 1, "aik": 1, "ik": 1,
    "do": 2, "two": 2, "dou": 2,
    "teen": 3, "three": 3, "tin": 3,
    "char": 4, "four": 4,
    "panch": 5, "five": 5,
    "chay": 6, "six": 6,
    "1st": 1, "2nd": 1, "3rd": 1, "4th": 1, "5th": 1,
    "first": 1, "second": 1, "third": 1, "fourth": 1,
}

SIZE_HINTS = [
    "family pack", "half plate", "full plate", "quarter plate",
    "half plat", "full plat",
    "0.5kg", "1.5kg", "2kg", "1kg", "0.25kg", "half kg", "1 kg", "2 kg",
    "500ml", "1.5l", "1.5L", "1l",
    "small", "medium", "large", "regular", "xl", "xxl",
    "half", "full",
]

_BUTTON_TEXTS = {
    "view menu 📋", "place order 🛒", "contact us 📞",
    "✅ confirm order", "➕ add more", "🗑️ clear cart",
    "✅ order now", "📋 view menu", "✅ order now",
    "order again 🔄", "view menu", "place order", "contact us",
    "confirm order", "add more", "clear cart", "order now",
    # reservation buttons
    "🪑 book a table", "📅 my reservations", "✅ confirm reservation",
    "❌ cancel reservation", "book a table", "my reservations",
    "confirm reservation", "cancel reservation",
}
