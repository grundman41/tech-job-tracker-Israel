# הפרופיל שלי: `iem_saas_ops`

חיפוש רחב בכוונה, לבוגר הנדסת תעשייה וניהול עם כשנתיים בצוות אופרציה בסטארטאפ SaaS.

| הגדרה | ערך |
|---|---|
| ניסיון | 0–4 שנים. משרה שדורשת 5+ שנים נפסלת. משרה של 2–4 שנים מדורגת **גבוה**, משרת סטודנט מדורגת נמוך. |
| ערים | תל אביב (כולל יפו), רמת גן, בני ברק, גבעתיים, הרצליה (כולל הרצליה פיתוח) |
| חסומים | בנקים בלבד. חברות ביטחוניות **כן** יוצגו. |
| סטודנטים | **נפסלות** — משרות סטודנט, התמחות ו-Trainee לא יופיעו כלל |
| "Senior" בכותרת | **נפסל** — Senior, Sr., Lead, Staff, Expert, בכיר |
| משרות ניהול | נפסלות: Head of, Director, VP, ראש צוות, מנהל מחלקה |
| משרות זמניות | **נפסלות** — Temporary, Fixed-Term, Maternity Cover, Interim, Seasonal, Freelance, חל"ד, משרה זמנית |

---

## 8 הקטגוריות שהמערכת מחפשת

### 1. תפעול ו-Operations (הליבה)
Operations Analyst / Specialist / Manager / Coordinator · Business Operations · BizOps · Strategy & Operations · Customer Operations · Support Operations · Product Operations · Partner Operations · Operational Excellence

### 2. ניהול פרויקטים ותוכניות
Project Manager · Program Manager · Technical Program Manager (TPM) · PMO / PMO Analyst · Project Coordinator · Project Analyst · Implementation Project Manager · Delivery Manager

### 3. מוצר
Product Manager · Associate Product Manager (APM) · Product Owner · Product Analyst · Product Specialist

### 4. אנליזה עסקית
Business Analyst · Business Systems Analyst · Systems Analyst · Business Process Analyst · Strategy Analyst · Operations Research Analyst

### 5. Customer Success והטמעה
Customer Success Manager · Onboarding Manager / Specialist · Implementation Specialist / Consultant / Manager · Account Manager · Technical Account Manager · Customer Experience Manager · Professional Services Consultant

### 6. Solutions
Solutions Consultant · Solutions Engineer · Solutions Architect (ללא קוד) · Forward Deployed · Deployment Specialist

### 7. דאטה ו-BI — **עדיפות נמוכה**
Data Analyst · BI Analyst · Business Intelligence Analyst · Analytics Specialist

### 8. פיננסים — **עדיפות נמוכה**
Financial Analyst · FP&A Analyst · Pricing Analyst · FinOps Analyst

### 9. פיתוח עסקי
Business Development Manager / Analyst

> קטגוריות 7 ו-8 מקבלות ציון התאמה מוכפל ב-0.6, ולכן יופיעו מתחת לתפקידי הליבה בלוח. לשינוי: השדה `role_category_weights`.

---

## מה נפסל תמיד

**לפי בקשתך הוסרו:** RevOps · Sales Operations · GTM Operations · Marketing Operations · Deal Desk · Billing / Payment Operations · Risk / Fraud / Trust & Safety · Workforce Management · Capacity Planning · Chief of Staff · Founder's Associate · מערכות עסקיות ואוטומציה (Salesforce, CRM, NetSuite, RPA, No-Code) · שיפור תהליכים, Lean / Six Sigma והנדסת תעשייה קלאסית · רכש, שרשרת אספקה ולוגיסטיקה · שותפויות · ייעוץ (חברות ייעוץ) · **משרות סטודנט, התמחות ו-Trainee**

> `Solutions Consultant` ו-`Implementation Consultant` **כן** עוברים, למרות המילה "Consultant", כי אלה תפקידי הטמעה בחברות מוצר.

**נפסל ממילא:** פיתוח תוכנה (Backend / Frontend / Fullstack / Mobile) · Data Engineering / Data Science / ML / AI Engineering · אלגוריתמים ומחקר · חומרה ואמבדד · QA ואוטומציית בדיקות · DevOps / SRE / IT · סייבר ו-SOC · תמיכה טכנית ושירות לקוחות בחזית (אבל **Support Operations מותר**) · מכירות טלפוניות (SDR / BDR) · שיווק מכל סוג · משאבי אנוש וגיוס · משפטי · הנהלת חשבונות ושכר · בנקים

---

## לשנות בעצמך

הכול בקובץ `config.json`, תחת `profiles.iem_saas_ops`:

| מה לשנות | שדה |
|---|---|
| להוסיף או להסיר עיר | `allowed_cities` — תמיד באנגלית **ובעברית** |
| טווח הניסיון | `max_yoe` |
| להוריד קטגוריה מעדיפות | `role_category_weights` |
| לחסום חברה | `excluded_companies` |
| לבטל את חסימת הבנקים | למחוק את הקבוצה `banks` |
| חיפושים בלינקדאין | `linkedin_queries` |
