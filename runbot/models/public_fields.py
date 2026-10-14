from odoo import models


class PublicFieldsMixin(models.AbstractModel):
    _name = 'runbot.public.fields.mixin'
    _description = 'Restrict non runbot users to the public fields'

    _public_fields = ('id', 'display_name')

    def _has_field_access(self, field, operation):
        if field.name not in self._public_fields and not self.env.su and not self.env.user.has_group('runbot.group_user'):
            return False
        return super()._has_field_access(field, operation)
