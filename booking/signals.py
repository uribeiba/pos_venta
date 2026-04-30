# booking/signals.py
# COMENTAR TEMPORALMENTE ESTA SEÑAL

# @receiver(post_save, sender=User, dispatch_uid="booking.ensure_userprofile_once")
# def ensure_userprofile_once(sender, instance, created: bool, **kwargs):
#     """
#     Garantiza EXACTAMENTE un UserProfile por User.
#     """
#     if created:
#         try:
#             UserProfile.objects.create(
#                 user=instance,
#                 role="vendedor",
#                 is_active=True
#             )
#         except IntegrityError:
#             # Si ya existe, ignorar
#             pass