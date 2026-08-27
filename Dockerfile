FROM python:3.11-slim

# System libraries WeasyPrint needs for PDF/print rendering (Pango, Cairo,
# GDK-Pixbuf) -- this is exactly why we can't run this on a plain serverless
# function; a real container gives us apt-get.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000
EXPOSE 10000

# 2 workers is plenty for single-agent, once-a-day use; keep it light so it
# fits comfortably in Render's free-tier memory limit. Timeout raised well
# past the default -- batch/mailer mode parses + renders every file in ONE
# request, and a real-world batch of ~50 Remine PDFs (each needing a
# pdfplumber parse + a 2-page WeasyPrint render) can easily run past 60s,
# especially with the free-tier's cold-start delay stacked on top. Gunicorn
# killing the worker mid-batch was surfacing as a cryptic "<html>... is not
# valid JSON" alert in the browser (the frontend expected a JSON error body
# and got Render's own gateway error page instead) -- see the matching fix
# in app.py's fetch() error handling for the other half of that.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 600
