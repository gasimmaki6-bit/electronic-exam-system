import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(BASE_DIR, "exam_system.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # إعدادات منع الغش
    MAX_TAB_SWITCH_VIOLATIONS = 3   # عدد مرات الخروج من الشاشة قبل التسليم التلقائي

    # إعدادات رفع صور الأسئلة
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 ميجابايت كحد أقصى لحجم الصورة
