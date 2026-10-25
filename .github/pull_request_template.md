## Description
<!-- Provide a brief description of the changes in this pull request -->

## Motivation & Context
<!-- Why is this change required? What problem does it solve? If it fixes an open issue, link it here: e.g. Fixes #123 -->

## Type of Change
- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Refactoring / Performance improvement

## Checklist
- [ ] My code adheres to the project's code style and conventions
- [ ] Backend tests pass (`cd backend && pytest`)
- [ ] Frontend tests pass (`cd frontend && npm test`)
- [ ] Frontend builds without TypeScript or bundling errors (`cd frontend && npm run build`)
- [ ] Translation scripts pass (`python3 scripts/check_i18n_keys.py && python3 scripts/check_neutral_spanish.py`)
- [ ] Error codes check passes (`python3 scripts/check_error_codes.py`)
- [ ] Database schema changes include a corresponding Alembic migration (`alembic revision --autogenerate`)
- [ ] No private secrets, tokens, or environment files are included
