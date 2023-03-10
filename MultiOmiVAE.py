import torch
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split
from torch import nn, optim
from torch.utils.data import Dataset, DataLoader
from torch.nn import functional as F
from torch.utils.tensorboard import SummaryWriter
from earlystoping import Earlystopping
from sklearn import metrics

# Here you will fine the MultiOmiVAE function which is an adapatation according to the original one. Instead of using DNA methylation, I used DNA mutation 
# without chromosome listing. So the architecture was modiofied as the ExprOmiVAE. Genes x Samples is needed for all data.
def MultiOmiVAE(input_path, expr_df, methy_df_list, random_seed=42, no_cuda=False, model_parallelism=False,
                separate_testing=True, batch_size=32, latent_dim=128, learning_rate=1e-3, p1_epoch_num=50,
                p2_epoch_num=100, output_loss_record=True, classifier=True, early_stopping=True):

    torch.manual_seed(random_seed)
    torch.cuda.manual_seed_all(random_seed)

    device = torch.device('cuda:0' if not no_cuda and torch.cuda.is_available() else 'cpu')
    parallel = torch.cuda.device_count() > 1 and model_parallelism

    # Sample ID and order that has both gene expression and DNA mutation data
    sample_id=pd.read_csv("path/labels.csv", sep=",", index_col=0, dtype='str')

    # Loading label
    label = pd.read_csv('path/labels.csv', sep=',',index_col=0)
    class_num = len(label.response.unique())
    label_array = label['response'].values

    if separate_testing:
        # Get testing set index and training set index
        # Separate according to different tumour types
        testset_ratio = 0.2
        valset_ratio = 0.5

        train_index, test_index, train_label, test_label = train_test_split(sample_id, label_array,
                                                                            test_size=testset_ratio,
                                                                            random_state=random_seed,
                                                                            stratify=label_array)
        val_index, test_index, val_label, test_label = train_test_split(test_index, test_label, test_size=valset_ratio,
                                                                        random_state=random_seed, stratify=test_label)

        expr_df_test = expr_df[test_index.index]
        expr_df_val = expr_df[val_index.index]
        expr_df_train = expr_df[train_index.index]
        
        methy_df_list=expr_df
        #methy_df_list=methy_chr_df_list
        methy_chr_df_test = methy_df_list[test_index.index]
        methy_chr_df_val = methy_df_list[val_index.index]
        methy_chr_df_train = methy_df_list[train_index.index]

    # Get multi-omics dataset information
    sample_num = len(sample_id)
    expr_feature_num = expr_df.shape[0]
    
    methy_feature_num = methy_df_list.shape[0]
    print('\nNumber of samples: {}'.format(sample_num))
    print('Number of gene expression features: {}'.format(expr_feature_num))
    print('Number of methylation features: {}'.format(methy_feature_num))
    if classifier:
        print('Number of classes: {}'.format(class_num))

    class MultiOmiDataset(Dataset):
        """
        Load multi-omics data
        """

        def __init__(self, expr_df, methy_df_list, labels):
            self.expr_df = expr_df
            self.methy_df_list = methy_df_list
            self.labels = labels

        def __len__(self):
            return self.expr_df.shape[1]

        def __getitem__(self, index):
            omics_data = []
            # Gene expression tensor index 0
            expr_line = self.expr_df.iloc[:, index].values
            expr_line_tensor = torch.Tensor(expr_line)
            omics_data.append(expr_line_tensor)
            # Methylation tensor index 1-23
            
            methy_chr_line = self.methy_df_list.iloc[:, index].values
            methy_chr_line_tensor = torch.Tensor(methy_chr_line)
            omics_data.append(methy_chr_line_tensor)
            label = self.labels[index]
            return [omics_data, label]

    # DataSets and DataLoaders
    if separate_testing:
        train_dataset = MultiOmiDataset(expr_df=expr_df_train, methy_df_list=methy_chr_df_train, labels=train_label)
        train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
        val_dataset = MultiOmiDataset(expr_df=expr_df_val, methy_df_list=methy_chr_df_val, labels=val_label)
        val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=True)
        test_dataset = MultiOmiDataset(expr_df=expr_df_test, methy_df_list=methy_chr_df_test, labels=test_label)
        test_loader = DataLoader(test_dataset, batch_size=batch_size, shuffle=True)
    else:
        train_dataset = MultiOmiDataset(expr_df=expr_df, methy_df_list=methy_df_list, labels=label_array)
        train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    full_dataset = MultiOmiDataset(expr_df=expr_df, methy_df_list=methy_df_list, labels=label_array)
    full_loader = DataLoader(full_dataset, batch_size=batch_size, num_workers=6)

    # Setting dimensions
    latent_space_dim = latent_dim
    input_dim_expr = expr_feature_num
    input_dim_methy = methy_feature_num
    level_2_dim_expr = 4096
    level_2_dim_methy = 256
    level_3_dim_expr = 1024
    level_3_dim_methy = 1024
    level_4_dim = 512
    classifier_1_dim = 128
    classifier_2_dim = 64
    classifier_out_dim = class_num

    class VAE(nn.Module):
        def __init__(self):
            super(VAE, self).__init__()
            # ENCODER fc layers
            # level 1
            # Methy input for each chromosome
            self.e_fc1_methy = self.fc_layer(input_dim_methy, level_2_dim_methy)
            

            # Expr input
            self.e_fc1_expr = self.fc_layer(input_dim_expr, level_2_dim_expr)

            # Level 2
            self.e_fc2_methy = self.fc_layer(level_2_dim_methy, level_3_dim_methy)
            self.e_fc2_expr = self.fc_layer(level_2_dim_expr, level_3_dim_expr)
            # self.e_fc2_methy = self.fc_layer(level_2_dim_methy * 23, level_3_dim_methy, dropout=True)
            # self.e_fc2_expr = self.fc_layer(level_2_dim_expr, level_3_dim_expr, dropout=True)

            # Level 3
            self.e_fc3 = self.fc_layer(level_3_dim_methy+level_3_dim_expr, level_4_dim)
            # self.e_fc3 = self.fc_layer(level_3_dim_methy+level_3_dim_expr, level_4_dim, dropout=True)

            # Level 4
            self.e_fc4_mean = self.fc_layer(level_4_dim, latent_space_dim, activation=0)
            self.e_fc4_log_var = self.fc_layer(level_4_dim, latent_space_dim, activation=0)

            # model parallelism
            if parallel:
                self.e_fc1_methy.to('cuda:0')
                self.e_fc1_expr.to('cuda:0')
                self.e_fc2_methy.to('cuda:0')
                self.e_fc2_expr.to('cuda:0')
                self.e_fc3.to('cuda:0')
                self.e_fc4_mean.to('cuda:0')
                self.e_fc4_log_var.to('cuda:0')

            # DECODER fc layers
            # Level 4
            self.d_fc4 = self.fc_layer(latent_space_dim, level_4_dim)

            # Level 3
            self.d_fc3 = self.fc_layer(level_4_dim, level_3_dim_methy+level_3_dim_expr)
            # self.d_fc3 = self.fc_layer(level_4_dim, level_3_dim_methy+level_3_dim_expr, dropout=True)

            # Level 2
            self.d_fc2_methy = self.fc_layer(level_3_dim_methy, level_2_dim_methy)
            self.d_fc2_expr = self.fc_layer(level_3_dim_expr, level_2_dim_expr)
            # self.d_fc2_methy = self.fc_layer(level_3_dim_methy, level_2_dim_methy*23, dropout=True)
            # self.d_fc2_expr = self.fc_layer(level_3_dim_expr, level_2_dim_expr, dropout=True)

            # level 1
            # Methy output for each chromosome
            self.d_fc1_methy = self.fc_layer(level_2_dim_methy, input_dim_methy, activation=2)
            
            # Expr output
            self.d_fc1_expr = self.fc_layer(level_2_dim_expr, input_dim_expr, activation=2)

            # model parallelism
            if parallel:
                self.d_fc4.to('cuda:1')
                self.d_fc3.to('cuda:1')
                self.d_fc2_methy.to('cuda:1')
                self.d_fc2_expr.to('cuda:1')
                self.d_fc1_methy.to('cuda:1')
                
                self.d_fc1_expr.to('cuda:1')

            # CLASSIFIER fc layers
            self.c_fc1 = self.fc_layer(latent_space_dim, classifier_1_dim)
            self.c_fc2 = self.fc_layer(classifier_1_dim, classifier_2_dim)
            # self.c_fc2 = self.fc_layer(classifier_1_dim, classifier_2_dim, dropout=True)
            self.c_fc3 = self.fc_layer(classifier_2_dim, classifier_out_dim, activation=0)

            # model parallelism
            if parallel:
                self.c_fc1.to('cuda:1')
                self.c_fc2.to('cuda:1')
                self.c_fc3.to('cuda:1')

        # Activation - 0: no activation, 1: ReLU, 2: Sigmoid
        def fc_layer(self, in_dim, out_dim, activation=1, dropout=False, dropout_p=0.5):
            if activation == 0:
                layer = nn.Sequential(
                    nn.Linear(in_dim, out_dim),
                    nn.BatchNorm1d(out_dim))
            elif activation == 2:
                layer = nn.Sequential(
                    nn.Linear(in_dim, out_dim),
                    nn.BatchNorm1d(out_dim),
                    nn.Sigmoid())
            else:
                if dropout:
                    layer = nn.Sequential(
                        nn.Linear(in_dim, out_dim),
                        nn.BatchNorm1d(out_dim),
                        nn.ReLU(),
                        nn.Dropout(p=dropout_p))
                else:
                    layer = nn.Sequential(
                        nn.Linear(in_dim, out_dim),
                        nn.BatchNorm1d(out_dim),
                        nn.ReLU())
            return layer

        def encode(self, x):
            

            # concat methy tensor together
            methy_level2_layer = self.e_fc1_methy(x[0])

            expr_level2_layer = self.e_fc1_expr(x[0])

            methy_level3_layer = self.e_fc2_methy(methy_level2_layer)
            expr_level3_layer = self.e_fc2_expr(expr_level2_layer)
            level_3_layer = torch.cat((methy_level3_layer, expr_level3_layer), 1)

            level_4_layer = self.e_fc3(level_3_layer)

            latent_mean = self.e_fc4_mean(level_4_layer)
            latent_log_var = self.e_fc4_log_var(level_4_layer)

            return latent_mean, latent_log_var

        def reparameterize(self, mean, log_var):
            sigma = torch.exp(0.5 * log_var)
            eps = torch.randn_like(sigma)
            return mean + eps * sigma

        def decode(self, z):
            level_4_layer = self.d_fc4(z)

            level_3_layer = self.d_fc3(level_4_layer)
            methy_level3_layer = level_3_layer.narrow(1, 0, level_3_dim_methy)
            expr_level3_layer = level_3_layer.narrow(1, level_3_dim_methy, level_3_dim_expr)

            methy_level2_layer = self.d_fc2_methy(methy_level3_layer)
            

            expr_level2_layer = self.d_fc2_expr(expr_level3_layer)

            recon_x0 = self.d_fc1_expr(expr_level2_layer)

            recon_x1 = self.d_fc1_methy(methy_level2_layer)
            

            return [recon_x0, recon_x1]

        def classifier(self, mean):
            level_1_layer = self.c_fc1(mean)
            level_2_layer = self.c_fc2(level_1_layer)
            output_layer = self.c_fc3(level_2_layer)
            return output_layer

        def forward(self, x):
            mean, log_var = self.encode(x)
            z = self.reparameterize(mean, log_var)
            classifier_x = mean
            if parallel:
                z = z.to('cuda:1')
                classifier_x = classifier_x.to('cuda:1')
            recon_x = self.decode(z)
            pred_y = self.classifier(classifier_x)
            return z, recon_x, mean, log_var, pred_y

    # Instantiate VAE
    if parallel:
        vae_model = VAE()
    else:
        vae_model = VAE().to(device)

    # Early Stopping
    if early_stopping:
        early_stop_ob = Earlystopping()

    # Tensorboard writer
    train_writer = SummaryWriter(log_dir='logs/train')
    val_writer = SummaryWriter(log_dir='logs/val')

    # print the model information
    # print('\nModel information:')
    # print(vae_model)
    total_params = sum(params.numel() for params in vae_model.parameters())
    print('Number of parameters: {}'.format(total_params))

    optimizer = optim.Adam(vae_model.parameters(), lr=learning_rate)

    def methy_recon_loss(recon_x, x):
        loss = F.binary_cross_entropy(recon_x[0], x[0], reduction='sum')
        
        return loss

    def expr_recon_loss(recon_x, x):
        loss = F.binary_cross_entropy(recon_x[0], x[0], reduction='sum')
        return loss

    def kl_loss(mean, log_var):
        loss = -0.5 * torch.sum(1 + log_var - mean.pow(2) - log_var.exp())
        return loss

    def classifier_loss(pred_y, y):
        loss = F.cross_entropy(pred_y, y, reduction='sum')
        return loss

    # k_methy_recon = 1
    # k_expr_recon = 1
    # k_kl = 1
    # k_class = 1

    # loss record
    loss_array = np.zeros(shape=(11, p1_epoch_num+p2_epoch_num+1))
    # performance metrics
    metrics_array = np.zeros(4)

    def train(e_index, e_num, k_methy_recon, k_expr_recon, k_kl, k_c):
        vae_model.train()
        train_methy_recon = 0
        train_expr_recon = 0
        train_kl = 0
        train_classifier = 0
        train_correct_num = 0
        train_total_loss = 0
        for batch_index, sample in enumerate(train_loader):
            data = sample[0]
            y = sample[1]
            
            data = data
            y = y
            optimizer.zero_grad()
            _, recon_data, mean, log_var, pred_y = vae_model(data)
            if parallel:
                
                recon_data = recon_data.to('cuda:0')
                pred_y = pred_y.to('cuda:0')

            methy_recon = methy_recon_loss(recon_data, data)
            expr_recon = expr_recon_loss(recon_data, data)
            kl = kl_loss(mean, log_var)
            class_loss = classifier_loss(pred_y, y)
            loss = k_methy_recon * methy_recon + k_expr_recon * expr_recon + k_kl * kl + k_c * class_loss

            loss.backward()

            with torch.no_grad():
                pred_y_softmax = F.softmax(pred_y, dim=1)
                _, predicted = torch.max(pred_y_softmax, 1)
                correct = (predicted == y).sum().item()

                train_methy_recon += methy_recon.item()
                train_expr_recon += expr_recon.item()
                train_kl += kl.item()
                train_classifier += class_loss.item()
                train_correct_num += correct
                train_total_loss += loss.item()

            optimizer.step()

            # if batch_index % 15 == 0:
            #     print('Epoch {:3d}/{:3d}  ---  [{:5d}/{:5d}] ({:2d}%)\n'
            #           'Methy Recon Loss: {:.2f}   Expr Recon Loss: {:.2f}   KL Loss: {:.2f}   '
            #           'Classification Loss: {:.2f}\nACC: {:.2f}%'.format(
            #         e_index + 1, e_num, batch_index * len(data[0]), len(train_dataset),
            #         round(100. * batch_index / len(train_loader)), methy_recon.item() / len(data[0]),
            #         expr_recon.item() / len(data[0]), kl.item() / len(data[0]), class_loss.item() / len(data[0]),
            #         correct / len(data[0]) * 100))

        train_methy_recon_ave = train_methy_recon / len(train_dataset)
        train_expr_recon_ave = train_expr_recon / len(train_dataset)
        train_kl_ave = train_kl / len(train_dataset)
        train_classifier_ave = train_classifier / len(train_dataset)
        train_accuracy = train_correct_num / len(train_dataset) * 100
        train_total_loss_ave = train_total_loss / len(train_dataset)

        print('Epoch {:3d}/{:3d}\n'
              'Training\n'
              'Methy Recon Loss: {:.2f}   Expr Recon Loss: {:.2f}   KL Loss: {:.2f}   '
              'Classification Loss: {:.2f}\nACC: {:.2f}%'.
              format(e_index + 1, e_num, train_methy_recon_ave, train_expr_recon_ave, train_kl_ave,
                     train_classifier_ave, train_accuracy))
        loss_array[0, e_index] = train_methy_recon_ave
        loss_array[1, e_index] = train_expr_recon_ave
        loss_array[2, e_index] = train_kl_ave
        loss_array[3, e_index] = train_classifier_ave
        loss_array[4, e_index] = train_accuracy

        # TB
        train_writer.add_scalar('Total loss', train_total_loss_ave, e_index)
        train_writer.add_scalar('Methy recon loss', train_methy_recon_ave, e_index)
        train_writer.add_scalar('Expr recon loss', train_expr_recon_ave, e_index)
        train_writer.add_scalar('KL loss', train_kl_ave, e_index)
        train_writer.add_scalar('Classification loss', train_classifier_ave, e_index)
        train_writer.add_scalar('Accuracy', train_accuracy, e_index)

    if separate_testing:
        def val(e_index, get_metrics=False):
            vae_model.eval()
            val_methy_recon = 0
            val_expr_recon = 0
            val_kl = 0
            val_classifier = 0
            val_correct_num = 0
            val_total_loss = 0
            y_store = torch.tensor([0])
            predicted_store = torch.tensor([0])

            with torch.no_grad():
                for batch_index, sample in enumerate(val_loader):
                    data = sample[0]
                    y = sample[1]
                    
                    data = data
                    y = y.to(device)
                    _, recon_data, mean, log_var, pred_y = vae_model(data)
                    if parallel:
                        for chr_i in range(24):
                            recon_data[chr_i] = recon_data[chr_i].to('cuda:0')
                        pred_y = pred_y.to('cuda:0')

                    methy_recon = methy_recon_loss(recon_data, data)
                    expr_recon = expr_recon_loss(recon_data, data)
                    kl = kl_loss(mean, log_var)
                    class_loss = classifier_loss(pred_y, y)
                    loss = methy_recon + expr_recon + kl + class_loss

                    pred_y_softmax = F.softmax(pred_y, dim=1)
                    _, predicted = torch.max(pred_y_softmax, 1)
                    correct = (predicted == y).sum().item()

                    y_store = torch.cat((y_store, y.cpu()))
                    predicted_store = torch.cat((predicted_store, predicted.cpu()))

                    val_methy_recon += methy_recon.item()
                    val_expr_recon += expr_recon.item()
                    val_kl += kl.item()
                    val_classifier += class_loss.item()
                    val_correct_num += correct
                    val_total_loss += loss.item()

            output_y = y_store[1:].numpy()
            output_pred_y = predicted_store[1:].numpy()

            if get_metrics:
                metrics_array[0] = metrics.accuracy_score(output_y, output_pred_y)
                metrics_array[1] = metrics.precision_score(output_y, output_pred_y, average='weighted')
                metrics_array[2] = metrics.recall_score(output_y, output_pred_y, average='weighted')
                metrics_array[3] = metrics.f1_score(output_y, output_pred_y, average='weighted')

            val_methy_recon_ave = val_methy_recon / len(val_dataset)
            val_expr_recon_ave = val_expr_recon / len(val_dataset)
            val_kl_ave = val_kl / len(val_dataset)
            val_classifier_ave = val_classifier / len(val_dataset)
            val_accuracy = val_correct_num / len(val_dataset) * 100
            val_total_loss_ave = val_total_loss / len(val_dataset)

            print('Validation\n'
                  'Methy Recon Loss: {:.2f}   Expr Recon Loss: {:.2f}   KL Loss: {:.2f}   Classification Loss: {:.2f}'
                  '\nACC: {:.2f}%\n'.
                  format(val_methy_recon_ave, val_expr_recon_ave, val_kl_ave, val_classifier_ave, val_accuracy))
            loss_array[5, e_index] = val_methy_recon_ave
            loss_array[6, e_index] = val_expr_recon_ave
            loss_array[7, e_index] = val_kl_ave
            loss_array[8, e_index] = val_classifier_ave
            loss_array[9, e_index] = val_accuracy

            # TB
            val_writer.add_scalar('Total loss', val_total_loss_ave, e_index)
            val_writer.add_scalar('Methy recon loss', val_methy_recon_ave, e_index)
            val_writer.add_scalar('Expr recon loss', val_expr_recon_ave, e_index)
            val_writer.add_scalar('KL loss', val_kl_ave, e_index)
            val_writer.add_scalar('Classification loss', val_classifier_ave, e_index)
            val_writer.add_scalar('Accuracy', val_accuracy, e_index)

            return val_accuracy, output_pred_y

    print('\nUNSUPERVISED PHASE\n')
    # unsupervised phase
    for epoch_index in range(p1_epoch_num):
        train(e_index=epoch_index, e_num=p1_epoch_num+p2_epoch_num, k_methy_recon=1, k_expr_recon=1, k_kl=1, k_c=0)
        if separate_testing:
            _, out_pred_y = val(epoch_index)

    print('\nSUPERVISED PHASE\n')
    # supervised phase
    epoch_number = p1_epoch_num
    for epoch_index in range(p1_epoch_num, p1_epoch_num+p2_epoch_num):
        epoch_number += 1
        train(e_index=epoch_index, e_num=p1_epoch_num+p2_epoch_num, k_methy_recon=0, k_expr_recon=0, k_kl=0, k_c=1)
        if separate_testing:
            if epoch_index == p1_epoch_num+p2_epoch_num-1:
                val_classification_acc, out_pred_y = val(epoch_index, get_metrics=True)
            else:
                val_classification_acc, out_pred_y = val(epoch_index)
            if early_stopping:
                early_stop_ob(vae_model, val_classification_acc)
                if early_stop_ob.stop_now:
                    print('Early stopping\n')
                    break

    if early_stopping:
        best_epoch = p1_epoch_num + early_stop_ob.best_epoch_num
        loss_array[10, 0] = best_epoch
        print('Load model of Epoch {:d}'.format(best_epoch))
        vae_model.load_state_dict(torch.load('/home/ldap/ltoure/OmiVAE/ssd/checkpoint.pt'))
        _, out_pred_y = val(epoch_number, get_metrics=True)

    # Encode all of the data into the latent space
    print('Encoding all the data into latent space...')
    vae_model.eval()
    with torch.no_grad():
        d_z_store = torch.zeros(1, latent_dim).to(device)
        for i, sample in enumerate(full_loader):
            d = sample[0]
            
            d = d
            _, _, d_z, _, _ = vae_model(d)
            d_z_store = torch.cat((d_z_store, d_z), 0)
    all_data_z = d_z_store[1:]
    all_data_z_np = all_data_z.cpu().numpy()

    # Output file
    print('Preparing the output files... ')
    input_path_name = input_path.split('/')[-1]
    latent_space_path = '/home/ldap/ltoure/OmiVAE/results/' + input_path_name + str(latent_dim) + 'D_latent_space.tsv'

    # Whether variable sample_id exists
    all_data_z_df = pd.DataFrame(all_data_z_np, index=sample_id)
    all_data_z_df.to_csv(latent_space_path, sep='\t')

    if separate_testing:
        pred_y_path =  '/home/ldap/ltoure/OmiVAE/results/' + input_path_name + str(latent_dim) + 'D_pred_y.tsv'
        np.savetxt(pred_y_path, out_pred_y, delimiter='\t')

        metrics_record_path = '/home/ldap/ltoure/OmiVAE/results/' + input_path_name + str(latent_dim) + 'D_metrics.tsv'
        np.savetxt(metrics_record_path, metrics_array, delimiter='\t')

    if output_loss_record:
        loss_record_path = '/home/ldap/ltoure/OmiVAE/results/' + input_path_name + str(latent_dim) + 'D_loss_record.tsv'
        np.savetxt(loss_record_path, loss_array, delimiter='\t')

    return all_data_z_df
