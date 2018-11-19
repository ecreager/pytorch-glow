CUDA_VISIBLE_DEVICES=0 python train.py \
	--depth 10 \
	--coupling affine \
	--batch_size 64 \
	--print_every 100 \
	--permutation conv \
	--depth 16 \
	--hidden_channels 128 \
	--sample_dir ./samples
 
