# -*- coding: utf-8 -*-
"""
سكربت تهيئة قاعدة البيانات.
يقوم بإنشاء الجداول، وإنشاء حساب مدير افتراضي إذا لم يوجد أي مدير.
شغّله مرة واحدة قبل تشغيل التطبيق:  python init_db.py
"""
import os
from app import app
from models import db, User, Question, Subject

DB_PATH = os.path.join(os.path.dirname(__file__), "exam_system.db")

with app.app_context():
    db_existed = os.path.exists(DB_PATH)
    db.create_all()

    # تحذير عند الترقية من نسخة سابقة: db.create_all() لا يضيف أعمدة جديدة
    # لجداول موجودة مسبقاً، ولا يعرف الجداول الجديدة كلياً في بعض حالات SQLite القديمة.
    if db_existed:
        try:
            Question.query.first()
            Subject.query.first()
        except Exception:
            print("⚠️  يبدو أنك تستخدم قاعدة بيانات من نسخة قديمة من النظام لا تحتوي")
            print("    على الجداول/الأعمدة الجديدة (المواد الدراسية، سجل التدقيق، الإشعارات،")
            print("    أسئلة الترتيب والإجابة القصيرة...).")
            print("    لحل المشكلة: أوقف الخادم، احذف ملف exam_system.db، ثم شغّل")
            print("    python init_db.py من جديد (ملاحظة: ستُفقد كل البيانات القديمة).")
            raise SystemExit(1)

    if not User.query.filter_by(role="admin").first():
        admin = User(
            full_name="مدير النظام",
            username="admin",
            role="admin",
        )
        admin.set_password("Admin@123")
        db.session.add(admin)
        db.session.commit()
        print("✅ تم إنشاء حساب المدير الافتراضي:")
        print("   اسم المستخدم: admin")
        print("   كلمة المرور: Admin@123")
        print("   (يرجى تغييرها فوراً بعد أول تسجيل دخول)")
    else:
        print("✅ قاعدة البيانات جاهزة، يوجد حساب مدير مسبقاً.")
