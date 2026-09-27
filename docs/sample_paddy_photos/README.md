# Sample paddy photos for testing "Diagnose plant"

Four real, openly-licensed photos for manually testing the plant-diagnosis feature (crop = Rice) without
needing a real field photo on hand. In the app: **Diagnose plant > Choose from gallery**.

| File | Shows | Expect roughly |
|---|---|---|
| `bacterial_leaf_blight.jpg` | Bacterial leaf blight (yellowing/streaking leaf blades) | Matches `dis_leaf_spot` (generic fungal/bacterial leaf spot or blight) |
| `brown_spot.jpg` | Brown spot (Cochliobolus miyabeanus), close-up of lesions | Matches `dis_leaf_spot` |
| `rice_blast.jpg` | Rice blast, diamond-shaped lesions on the blade (low-res historical plate) | Matches `dis_leaf_spot`, or `status: unclear` given the low resolution |
| `healthy_paddy_leaf.jpg` | Healthy green paddy leaf, no lesions | `status: healthy` |

There are no rice-specific condition IDs in `backend/app/data/remedies.json` yet (only a generic
`dis_leaf_spot` bucket plus a stem-borer pest entry that already mentions rice "dead hearts"/white
ear-heads) - Gemini classifies from the photo and picks the closest generic match, or `other`/`unclear`
if none fits well.

## Attribution (all require credit; none are AgriN's own work)

| File | Author | License | Source |
|---|---|---|---|
| `bacterial_leaf_blight.jpg` | Donald Groth, Louisiana State University AgCenter, Bugwood.org | CC BY 3.0 US | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Bacterial_blight_of_rice.jpeg) |
| `brown_spot.jpg` | Donald Groth, Louisiana State University AgCenter, Bugwood.org | CC BY 3.0 US | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Cochliobolus_miyabeanus.jpg) |
| `rice_blast.jpg` | R.K. Webster, USDA Agricultural Research Service | Public domain (US government work) | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Rice_blast.jpg) |
| `healthy_paddy_leaf.jpg` | Yahya | CC BY-SA 3.0 | [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Dew_drop_on_green_paddy_leaf.jpg) |

These are reference/test images only - not bundled into the app build (nothing under `app/assets`
references them), so they don't affect app size. Keep the attribution above if you redistribute them
outside this repo.
