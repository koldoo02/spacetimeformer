import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange, repeat

from time2vec import Time2Vec

from .extra_layers import ConvBlock, Flatten


class Embedding(nn.Module):
    def __init__(
        self,
        d_y,
        d_x,
        d_model,
        time_emb_dim=6,
        method="spatio-temporal",
        downsample_convs=0,
        start_token_len=0,
        null_value=None,
        pad_value=None,
        is_encoder: bool = True,
        position_emb="abs",
        data_dropout=None,
        max_seq_len=None,
        use_val: bool = True,
        use_time: bool = True,
        use_space: bool = True,
        use_given: bool = True,
    ):
        super().__init__()

        assert method in ["spatio-temporal", "temporal"]
        if data_dropout is None:
            self.data_drop = lambda y: y
        else:
            self.data_drop = data_dropout

        self.method = method

        time_dim = time_emb_dim * d_x
        self.time_emb = Time2Vec(d_x, embed_dim=time_dim)

        self.max_seq_len = max_seq_len
        self.position_emb = position_emb



        assert max_seq_len is not None
        self.local_emb = nn.Embedding(
            num_embeddings=max_seq_len, embedding_dim=d_model
        )

        y_emb_inp_dim = d_y if self.method == "temporal" else 1
        self.val_time_emb = nn.Linear(y_emb_inp_dim + time_dim, d_model)

        if self.method == "spatio-temporal":
            self.space_emb = nn.Embedding(num_embeddings=d_y, embedding_dim=d_model)
            split_length_into = d_y
        else:
            split_length_into = 1

        self.start_token_len = start_token_len
        self.given_emb = nn.Embedding(num_embeddings=2, embedding_dim=d_model)

        self.downsize_convs = nn.ModuleList(
            [ConvBlock(split_length_into, d_model) for _ in range(downsample_convs)]
        )

        self.d_model = d_model
        self.null_value = null_value
        self.pad_value = pad_value
        self.is_encoder = is_encoder

        # turning off parts of the embedding is only really here for ablation studies
        self.use_val = use_val
        self.use_time = use_time
        self.use_given = use_given
        self.use_space = use_space

    def __call__(self, x: torch.Tensor, y: torch.Tensor):
        emb = self.spatio_temporal_embed
        return emb(y=y, x=x)

    def make_mask(self, y):
        # we make padding-based masks here due to outdated
        # feature where the embedding randomly drops tokens by setting
        # them to the pad value as a form of regularization
        if self.pad_value is None:
            return None
        return (y == self.pad_value).any(-1, keepdim=True)
  
    def spatio_temporal_embed(self, y: torch.Tensor, x: torch.Tensor): #en este x --> tiempo, y --> espacio
        # full spatiotemporal emb method. lots of shape rearrange code
        # here to create artifically long (n_x x dim) spatiotemporal sequence
        batch, n_x, map, dy = y.shape

        # position emb ("local_emb")
        local_pos = repeat(
            torch.arange(n_x).to(y.device), f"n_x -> {batch}  (n_x {dy} {map})" #multiplica las ultimas tres dimensiones (map1(90)* map2(60) * n_x(4)) del array de espacio
        ) # (16, 400)

        # lookup pos emb "abs"
        local_emb = self.local_emb(local_pos.long())

        # time emb
        if not self.use_time:
            x = torch.zeros_like(x)
        x = torch.nan_to_num(x)
        x = repeat(x, f"batch n_x dx -> batch ({dy} {map} n_x) dx")
        time_emb = self.time_emb(x) #([16, 400, 48])
        # protect against NaNs in y, but keep track for Given emb
        true_null = torch.isnan(y)
        y = torch.nan_to_num(y)
        if not self.use_val:
            y = torch.zeros_like(y)

        # keep track of pre-dropout x for given emb
        y_original = y.clone()
        y_original = Flatten(y_original) #([16, 400, 1])
        y = self.data_drop(y)
        y = Flatten(y) #([16, 400, 1])

        mask = self.make_mask(y) #Si pad_value es None --> mask = None

        # concat time_emb, y --> FF --> val_time_emb
        val_time_inp = torch.cat((time_emb, y), dim=-1) #([16, 600, 49])
        val_time_emb = self.val_time_emb(val_time_inp) #([16, 600, 100])


        # "given" embedding
        if self.use_given:
            given = torch.ones((batch, n_x, map, dy)).long().to(x.device)  # start as True
            if not self.is_encoder:
                # mask missing values that need prediction...
                given[:, self.start_token_len :, :] = 0  # (False)

            # if y was NaN, set Given = False
            given *= ~true_null

            # flatten now to make the rest easier to figure out
            given = rearrange(given, "batch len map dy -> batch (len dy map)")

            # use given embeddings to identify data that was dropped out
            given *= (y == y_original).squeeze(-1)

            if self.null_value is not None:
                # mask null values that were set to a magic number in the dataset itself
                null_mask = (y != self.null_value).squeeze(-1)
                given *= null_mask

            given_emb = self.given_emb(given)
        else:
            given_emb = 0.0

        val_time_emb = local_emb + val_time_emb + given_emb #([42, 1600, 200]) tiempo

        if self.is_encoder:
            for conv in self.downsize_convs:
                val_time_emb = conv(val_time_emb)
                n_x //= 2

        # space embedding
        var_idx = repeat(
            torch.arange(dy).long().to(x.device), f"dy -> {batch} (dy {map} {n_x})"
        )
        var_idx_true = var_idx.clone() #([42, 1600])
        if not self.use_space:
            var_idx = torch.zeros_like(var_idx)
        space_emb = self.space_emb(var_idx) #([42, 1600, 200]) espacio

        return val_time_emb, space_emb, var_idx_true, mask
