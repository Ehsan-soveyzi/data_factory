# مرحله سوم: مدل اعتباری و پیش‌بینی داده تازه

شناخت داده، آماده‌سازی، مقایسه مدل‌ها، انتخاب آستانه، ارزیابی نهایی و تفسیر اجرا شده‌اند.
بخش نه، مسیر پیش‌بینی فایل تازه را تکمیل می‌کند. الزام هشت تست طبق درخواست کاربر اجرا نشده است؛
بنابراین انطباق کامل با همه الزامات صورت سؤال ادعا نمی‌شود.

| بخش چالش | بخش نوت‌بوک | خروجی اصلی |
|---|---|---|
| ۱: شناخت داده | ۴ | [شواهد شناخت](evidence/section_01_dataset_understanding.json) |
| ۲: گمشده‌ها | ۵ | [انتخاب سیاست](evidence/section_02_selected_policy.json) و تقسیم ثابت توسعه/نهایی |
| ۳: آماده‌سازی | ۶ | [Pipeline](../../src/data_factory/credit_preprocessing.py) با یادگیری فقط از آموزش |
| ۴: مهندسی ویژگی | ۷ | [ویژگی‌های منتخب](evidence/section_04_selected_features.json) و ممیزی نشت |
| ۵: مقایسه مدل‌ها | ۸ | [مقایسه](evidence/section_05_model_comparison.json) و احتمال‌های OOF توسعه |
| ۶: انتخاب آستانه | ۹ | [سیاست ثابت](evidence/section_06_selected_decision.json) بر اساس هزینه |
| ۷: ارزیابی | ۱۰ | [ارزیابی نهایی](evidence/section_07_evaluation.json) و مدل ذخیره‌شده |
| ۸: تفسیر | ۱۱ | [اهمیت/انصاف](evidence/section_08_interpretation.json) و [شناسنامه مدل](model_card.md) |
| ۹: مهندسی و تحویل | ۱۲ | [کد پیش‌بینی](../../src/data_factory/credit_inference.py)، CLI و [شواهد اجرا](evidence/section_09_inference.json)؛ تست‌ها مستثنا شده‌اند |

## نصب و اجرا

از ریشه پروژه و با Python 3.14:

```powershell
python -m pip install -r requirements.txt
python -m pip install --no-build-isolation -e .
python -m data_factory predict-credit --input stages/stage_03/examples/new_applicants.csv --output data/stage_03/predictions/new_applicants.csv
```

فایل نمونه شش پرونده **ساختگی** دارد. برای داده خودت فقط مسیرهای `--input` و `--output` را عوض کن.
این مسیر نیازی به دیتابیس یا `.env` ندارد و مدل را دوباره آموزش نمی‌دهد. `--batch-size` پیش‌فرض ۱۰۰۰۰ است.
مدل پیش‌فرض [فایل joblib بخش هفت](evidence/section_07_fitted_classifier.joblib) است؛ با `--model` می‌توان مسیرش را تعیین کرد.
فایل `section_07_evaluation.json` باید کنار مدل بماند تا هش و دستور ثابت بررسی شوند.
نسخه scikit-learn مدل با نسخه نصب‌شده باید یکسان باشد؛ نسخه اجراشده در `requirements.txt` ثبت شده است.

## قرارداد ورودی و خروجی

ورودی ده ویژگی خام با نام‌های [فایل نمونه](examples/new_applicants.csv) را می‌خواهد؛ ترتیب ستون‌ها مهم نیست.
`row_id` و `SeriousDlqin2yrs` اختیاری‌اند و در تصمیم مدل استفاده نمی‌شوند. خانه خالی برای مقدار گمشده مجاز استفاده کن.
کدهای دیرکرد ۹۶/۹۸ فقط داخل Pipeline گمشده می‌شوند؛ مقادیر جایگزین از آموزش قبلی‌اند.
فایل آماده‌سازی‌شده با ۱۸ ستون، ورودی این دستور نیست.

| ستون خروجی | معنی |
|---|---|
| `record_number` | شماره رکورد در فایل ورودی؛ ترتیب حفظ می‌شود |
| `row_id` | شناسه ورودی در صورت وجود |
| `probability_default` | احتمال خروجی مدل برای کلاس ۱ |
| `prediction` | تصمیم با آستانه ثابت؛ ۱ یعنی ریسک نکول |

خطای schema، ستون تکراری/اضافی، سطر خراب یا مقدار نامعتبر موجب شکست با کد ۴ می‌شود.
خروجی فقط پس از موفقیت همه دسته‌ها جایگزین می‌شود؛ در خطای میانی خروجی قبلی حفظ می‌شود.

```python
import pandas as pd
from data_factory.credit_inference import load_classifier, predict_frame

classifier, metadata = load_classifier()
predictions = predict_frame(pd.read_csv("stages/stage_03/examples/new_applicants.csv"), classifier)
```

## مدل و نتیجه ثبت‌شده

مدل منتخب `hist_gradient_boosting` با آستانه دقیق `0.07994084497831476` و هزینه `FN=12, FP=1` است.
Pipeline شامل ۱۰ ویژگی اصلی، ۵ نشانگر گمشده و ۳ ویژگی مشتق‌شده است؛ در مجموع ۱۸ ورودی مدل.
تحلیل‌های توسعه از PostgreSQL خوانده شده‌اند؛ CSV تازه فقط ورودی مسیر inference است.
۱۲۰۰۰۰ رکورد توسعه و ۲۹۹۹۹ رکورد نهایی ثابت‌اند. نتیجه نهایی: AUC برابر 0.8683،
Recall برابر 73.97٪، Precision برابر 24.40٪ و هزینه
361.945 در هزار؛ کاهش هزینه نسبت به «همه سالم» 54.87٪.
این نتایج با اجرای inference دوباره محاسبه یا تنظیم نمی‌شوند.

## داده، پایگاه داده و تحویل

اعتبارسنجی اولیه با دستور زیر انجام می‌شود:

```powershell
python -m data_factory validate-csv --config config/credit_validation.json
```

قرارداد در `config/credit_schema.json` و مسیرها در `config/credit_validation.json` هستند.
خروجی موفق از `data/stage_03/curated/validation/latest.json` پیدا می‌شود؛ `accepted.csv` فقط ۱۲ ستون قرارداد را دارد.
از ۱۵۰۰۰۰ رکورد، ۱۴۹۹۹۹ پذیرفته و یک رکورد با سن صفر قرنطینه شد. کدهای ۹۶/۹۸ و مقادیر گمشده در منبع حفظ شدند.
بخش‌های ۲ و ۳ نوت‌بوک، DDL و بارگذاری با SQLAlchemy در `credit.accepted` پایگاه `ds_learning` را انجام داده‌اند.
مشخصات اتصال از `.env` خوانده می‌شود. جزئیات در [گزارش](report.md) و [راهنمای قرارداد CSV](../../docs/csv_contract.md) هستند.

اجرای بخش ۱۲ در کرنل مستقل، شش خروجی در سه دسته دو‌ردیفی ساخته و برابری API/CLI را نشان داد.
مدل یا آستانه عوض نشد؛ داده، ارزیابی نهایی و شواهد قبلی حفظ شدند. نمایش جدول‌ها دارک و متن فارسی راست‌چین است.
تاریخچه موجود ۲۱ commit مرتبط دارد؛ تغییرات این تحویل هنوز commit یا push نشده‌اند.
**هشت تست الزامی صورت سؤال، طبق درخواست کاربر نوشته یا اجرا نشده‌اند.** تست‌های قدیمی پروژه مربوط به مراحل قبلی‌اند.
